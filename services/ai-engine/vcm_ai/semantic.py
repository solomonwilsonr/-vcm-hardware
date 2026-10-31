"""Semantic part search over the component catalog, on Appwrite VectorsDB.

:class:`~vcm_ai.catalog.Catalog` matches by MPN prefix, which is exact and unforgiving: it answers
"what is this part" and nothing else. The question an engineer actually asks when a part goes end of
life is the other one — *what could I put here instead* — and that question is not a prefix match.
"A dual-slot 300 W accelerator I can run on an existing 8-pin harness" names no part at all.

So the catalog is embedded once into a VectorsDB collection and searched by meaning. The result is
a *shortlist*, never an answer: a nearest-neighbour hit is a plausible candidate, and plausible is
not the standard VCM reports findings at. :func:`replacements_for` therefore runs every candidate
back through the deterministic facts already in the catalog — type, socket, connector, slot width,
PCIe generation, driver floor — and labels each one ``drop_in``, ``check`` or ``incompatible`` with
the specific reasons. Semantic search proposes; the rule data disposes.

Tested offline against a fake embedding and vector service (see ``tests/fake_appwrite.py``). It has
**not been run against a live Appwrite VectorsDB**.
"""
from __future__ import annotations

from vcm_graph.backends.appwrite_backend import AppwriteConfig, AppwriteConfigError

from .catalog import Catalog

COLLECTION = "vcm_parts"
VECTOR_FIELD = "embedding"
INDEX = "parts_cosine"
DEFAULT_MODEL = "nomic-embed-text"
BATCH = 32
PROBE = "probe"          # embedded once to discover the model's dimension

#: fields worth saying out loud in the embedded sentence, and how to phrase them
_PHRASES: list[tuple[str, str]] = [
    ("type", "{v}"),
    ("form_factor", "{v} form factor"),
    ("tdp_w", "{v} W TDP"),
    ("watts", "{v} W"),
    ("memory_gb", "{v} GB memory"),
    ("memory_type", "{v}"),
    ("capacity_gb", "{v} GB"),
    ("socket", "socket {v}"),
    ("pcie_gen", "PCIe gen {v}"),
    ("pcie_lanes", "x{v} lanes"),
    ("slot_width", "{v}-slot"),
    ("power_connector", "{v} power connector"),
    ("speed_gbps", "{v} Gbps"),
    ("min_driver", "driver {v} or newer"),
    ("min_cuda", "CUDA {v} or newer"),
    ("vout", "{v} V out"),
    ("package", "{v} package"),
    ("note", "{v}"),
]

#: facts that must agree for a candidate to be a drop-in, and how to explain a mismatch
_HARD = {
    "type": "different kind of part ({a} vs {b})",
    "socket": "different socket ({a} vs {b})",
    "power_connector": "needs a {b} connector, installed part uses {a}",
    "memory_type": "different memory generation ({a} vs {b})",
}


def part_name(part: dict) -> str:
    """The part's primary match key, which is how every other module names it."""
    return part["match"][0] if part.get("match") else ""


def part_text(part: dict) -> str:
    """One sentence describing a part, for embedding.

    Deterministic, so re-indexing an unchanged catalog produces identical vectors and the upsert is
    a no-op rather than a rewrite.
    """
    bits = [part_name(part)]
    aliases = [m for m in part.get("match", [])[1:] if m]
    for key, shape in _PHRASES:
        v = part.get(key)
        if v not in (None, "", []):
            bits.append(shape.format(v=v))
    if aliases:
        bits.append("also known as " + ", ".join(aliases))
    return ", ".join(b for b in bits if b)


def _doc_id(part: dict) -> str:
    """Appwrite document ids allow [A-Za-z0-9._-] and 36 characters; MPNs do not fit."""
    import hashlib
    return "p" + hashlib.sha256(part_name(part).upper().encode()).hexdigest()[:34]


def _vector(emb) -> list[float]:
    """The vector out of one embedding result, whatever shape the SDK hands back."""
    if isinstance(emb, dict):
        if emb.get("error"):
            raise AppwriteConfigError(f"embedding failed: {emb['error']}")
        return list(emb.get("embedding") or [])
    if getattr(emb, "error", ""):
        raise AppwriteConfigError(f"embedding failed: {emb.error}")
    return list(getattr(emb, "embedding", []) or [])


def _embeddings(result) -> list[list[float]]:
    raw = result["embeddings"] if isinstance(result, dict) else getattr(result, "embeddings", [])
    return [_vector(e) for e in raw]


def _docs(page) -> list[dict]:
    raw = page["documents"] if isinstance(page, dict) else getattr(page, "documents", [])
    out = []
    for d in raw:
        if isinstance(d, dict):
            out.append(d)
        else:
            data = getattr(d, "data", None)
            row = dict(data) if isinstance(data, dict) else {}
            row.setdefault("$id", getattr(d, "id", ""))
            out.append(row)
    return out


def services(cfg: AppwriteConfig):
    """Build the VectorsDB and Embeddings clients, with a readable error when unavailable."""
    cfg.require()
    try:
        from appwrite.client import Client
        from appwrite.services.embeddings import Embeddings
        from appwrite.services.vectors_db import VectorsDB
    except ImportError as exc:  # pragma: no cover - only without the optional dep
        raise AppwriteConfigError(
            "the 'appwrite' package is required for semantic part search: "
            "pip install -e '.[appwrite]'") from exc
    client = Client()
    client.set_endpoint(cfg.endpoint).set_project(cfg.project).set_key(cfg.api_key)
    return VectorsDB(client), Embeddings(client)


class SemanticCatalog:
    """The component catalog as a searchable vector collection.

    ``vectors`` and ``embeddings`` may be injected for testing; otherwise both are built from
    ``cfg``.
    """

    def __init__(self, cfg: AppwriteConfig | None = None, vectors=None, embeddings=None,
                 model: str = DEFAULT_MODEL, collection: str = COLLECTION):
        self.cfg = cfg or AppwriteConfig()
        self.db = self.cfg.database
        self.collection = collection
        self.model = model
        if vectors is None or embeddings is None:
            built = services(self.cfg)
            vectors = vectors or built[0]
            embeddings = embeddings or built[1]
        self.vectors, self.embeddings = vectors, embeddings

    # ---- embedding --------------------------------------------------
    #: scopes an API key needs beyond the TablesDB ones, and what fails without each
    SCOPES = ("embeddings.write", "vectors.read", "vectors.write")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches. Appwrite generates the vectors, so no model runs locally."""
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            try:
                out.extend(_embeddings(
                    self.embeddings.create_text_embeddings(texts[i:i + BATCH], self.model)))
            except Exception as exc:
                raise self._explain(exc) from exc
        return out

    @classmethod
    def _explain(cls, exc: Exception) -> Exception:
        """Turn an Appwrite scope rejection into something a reader can act on."""
        text = str(exc)
        if "missing scope" in text.lower():
            return AppwriteConfigError(
                "the Appwrite API key is missing scopes needed for semantic search. Add "
                + ", ".join(cls.SCOPES)
                + " to the key in the console (API Keys -> your key -> Scopes), then re-run. "
                  f"Appwrite said: {text}")
        return exc

    def dimension(self) -> int:
        """The model's vector width, discovered rather than hardcoded."""
        return len(self.embed([PROBE])[0])

    # ---- provisioning -----------------------------------------------
    def provision(self) -> dict:
        """Create the vector database, collection and cosine index. Idempotent."""
        from appwrite.enums.vectors_db_index_type import VectorsDBIndexType
        done: dict[str, list[str]] = {"created": [], "existed": []}

        def step(label: str, fn) -> None:
            try:
                fn()
                done["created"].append(label)
            except Exception as exc:
                if "already exists" in str(exc).lower() or "409" in str(exc):
                    done["existed"].append(label)
                else:
                    raise

        dim = self.dimension()
        done["dimension"] = dim
        done["model"] = self.model
        step(f"database:{self.db}", lambda: self.vectors.create(self.db, "VCM"))
        step(f"collection:{self.collection}",
             lambda: self.vectors.create_collection(self.db, self.collection, "VCM parts", dim))
        step(f"index:{INDEX}", lambda: self.vectors.create_index(
            self.db, self.collection, INDEX, VectorsDBIndexType.HNSW_COSINE, [VECTOR_FIELD]))
        return done

    # ---- indexing ---------------------------------------------------
    def index(self, catalog: Catalog | None = None) -> dict:
        """Embed every catalog part and upsert it. Safe to re-run; ids are content-free."""
        cat = catalog or Catalog.load()
        parts = [p for p in cat.parts if part_name(p)]
        texts = [part_text(p) for p in parts]
        vectors = self.embed(texts)
        docs = [{"$id": _doc_id(p), "mpn": part_name(p), "type": p.get("type", ""),
                 "reviewed": bool(p.get("reviewed")), "text": t, VECTOR_FIELD: v}
                for p, t, v in zip(parts, texts, vectors, strict=True)]
        for i in range(0, len(docs), BATCH):
            self.vectors.upsert_documents(self.db, self.collection, docs[i:i + BATCH])
        return {"indexed": len(docs), "model": self.model,
                "dimension": len(vectors[0]) if vectors else 0}

    # ---- search -----------------------------------------------------
    def similar(self, text: str, limit: int = 5, type_: str | None = None) -> list[dict]:
        """Catalog parts nearest to ``text`` in meaning, closest first."""
        from appwrite.query import Query
        vec = self.embed([text])[0]
        queries = [Query.vector_cosine(VECTOR_FIELD, vec), Query.limit(limit)]
        if type_:
            queries.insert(0, Query.equal("type", type_))
        try:
            page = self.vectors.create_query(self.db, self.collection, queries=queries)
        except Exception as exc:
            raise self._explain(exc) from exc
        return [{k: v for k, v in d.items() if k != VECTOR_FIELD} for d in _docs(page)]


# ---- shortlist, then check ------------------------------------------
def compatibility(installed: dict, candidate: dict) -> dict:
    """Compare a candidate against the installed part using catalog facts only.

    Nothing here is semantic. The vector search decided what was worth comparing; this decides
    whether it would actually fit, and says why when it would not.
    """
    blockers, checks = [], []
    for key, shape in _HARD.items():
        a, b = installed.get(key), candidate.get(key)
        if a and b and str(a).lower() != str(b).lower():
            (blockers if key in ("type", "socket") else checks).append(
                shape.format(a=a, b=b))

    for key, label, worse in (("tdp_w", "draws {b} W against {a} W", lambda a, b: b > a),
                              ("slot_width", "needs {b} slots against {a}",
                               lambda a, b: b > a),
                              ("pcie_lanes", "wants x{b} against x{a}", lambda a, b: b > a)):
        a, b = installed.get(key), candidate.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and worse(a, b):
            checks.append(label.format(a=a, b=b))

    for key, label in (("min_driver", "requires driver {b} (installed part needs only {a})"),
                       ("min_cuda", "requires CUDA {b} (installed part needs only {a})")):
        a, b = installed.get(key), candidate.get(key)
        if a and b and _version(b) > _version(a):
            checks.append(label.format(a=a, b=b))

    verdict = "incompatible" if blockers else ("check" if checks else "drop_in")
    return {"verdict": verdict, "blockers": blockers, "checks": checks}


def _version(v) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).replace("-", ".").split("."))


def replacements_for(sem: SemanticCatalog, mpn: str = "", value: str = "",
                     catalog: Catalog | None = None, limit: int = 5) -> dict:
    """Shortlist replacements for an installed part, each checked against catalog facts.

    The shortlist is semantic and therefore fallible; every entry carries its own verdict and the
    reasons behind it, so nothing here is reported as more certain than it is.
    """
    cat = catalog or Catalog.load()
    installed = cat.find(mpn=mpn, value=value)
    if installed is None:
        raise AppwriteConfigError(
            f"no catalog entry for {mpn or value!r}; cannot judge replacements for a part whose "
            "properties are unknown")
    hits = sem.similar(part_text(installed), limit=limit + 1, type_=installed.get("type"))
    out = []
    for hit in hits:
        part = cat.find(mpn=hit.get("mpn", ""))
        if part is None or part_name(part).upper() == part_name(installed).upper():
            continue
        out.append({"mpn": part_name(part), "text": hit.get("text", ""),
                    "reviewed": bool(part.get("reviewed")), **compatibility(installed, part)})
    return {"installed": part_name(installed), "type": installed.get("type", ""),
            "candidates": out[:limit],
            "caveat": "shortlist ranked by meaning, then checked against catalog facts; "
                      "catalog entries that are not reviewed may be wrong"}


__all__ = ["SemanticCatalog", "part_text", "part_name", "compatibility", "replacements_for",
           "COLLECTION", "VECTOR_FIELD", "DEFAULT_MODEL"]
