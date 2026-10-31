"""Example site rules for VCM. Enable with ``VCM_RULE_PLUGINS=site_rules:production_policy``
(with this directory on ``PYTHONPATH``), or ship them in a package under the ``vcm.rules``
entry-point group.

Each rule receives a ``RuleContext``: read ``ctx.before`` / ``ctx.after`` / ``ctx.meta`` /
``ctx.changed_refs``, look values up with ``ctx.value(ref, key)``, and report with ``ctx.add``.
Plugins can add ``rule_based`` or ``speculative`` findings, never ``verified``.
"""

#: driver branches this site has retired from production
RETIRED_DRIVER_BRANCHES = ("470", "495", "510")
#: the most power a single accelerator may draw in this site's racks
MAX_ACCELERATOR_W = 450


def production_policy(ctx) -> None:
    driver = str((ctx.meta.get("firmware") or {}).get("gpu_driver", ""))
    if driver.split(".")[0] in RETIRED_DRIVER_BRANCHES:
        ctx.add("warning", f"Driver branch {driver.split('.')[0]} is retired at this site",
                f"The node declares driver {driver}. Site policy requires a supported branch "
                "before any accelerator change is rolled out.",
                [{"type": "declared", "source": "node manifest", "gpu_driver": driver}])
    hot = [r for r in ctx.changed_refs
           if (ctx.value(r, "tdp_w") or 0) > MAX_ACCELERATOR_W]
    if hot:
        ctx.add("warning", f"{len(hot)} accelerator(s) above the site's {MAX_ACCELERATOR_W} W cap",
                f"Affected: {', '.join(hot)}. These racks are provisioned for at most "
                f"{MAX_ACCELERATOR_W} W per accelerator.",
                [ctx.cite(hot[0], ["tdp_w"])], hot)
