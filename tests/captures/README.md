# Real machine captures

Every folder here is a capture of a real machine that VCM must keep reading correctly. The test
suite (`tests/test_captures.py`) picks up new folders automatically: add one and it is a regression
test from then on.

## Adding one

```bash
# 1. Capture (read-only BMC account is enough)
export VCM_BMC_PASSWORD='...'
vcm inventory fetch-redfish https://<bmc-ip> --user <readonly-user> --out /tmp/bmc.json
nvidia-smi --query-gpu=index,name,serial,pci.bus_id,driver_version,power.draw,power.limit,\
pcie.link.gen.max,pcie.link.width.max --format=csv > /tmp/gpus.csv

# 2. Scrub it into a folder here (serials, hostnames, addresses, UUIDs, secrets)
vcm inventory sanitize --redfish /tmp/bmc.json --nvidia-smi /tmp/gpus.csv \
    --out tests/captures/<vendor>-<model>

# 3. READ the scrubbed files. The scrubber cannot know what is sensitive in free-text or
#    vendor-specific fields. Anything it flagged is listed on stderr.

# 4. See what VCM made of it, and fix expect.json's notes ("what machine is this?")
vcm inventory doctor --redfish tests/captures/<name>/redfish.json \
                     --nvidia-smi tests/captures/<name>/nvidia-smi.csv

# 5. If you edited any file, record that you read it, then check
vcm inventory seal tests/captures/<name>
vcm inventory check tests/captures/<name>
pytest tests/test_captures.py
```

Name folders by hardware (`supermicro-sys-420gp`, `dell-xe9680`), not by site.

## What the tests enforce

* **Sealed.** `expect.json` holds the sha256 of every capture file as `sanitize` wrote it. A raw
  capture pasted in, or a scrubbed file edited afterwards, fails until someone re-reads it and
  runs `vcm inventory seal`.
* **Nothing the scrubber always removes remains:** e-mail addresses, URL credentials, secrets.
* **VCM still reads it as expected:** component counts, covered types and firmware keys match
  `expect.json`, and the doctor reports no problems that `allow_problems` does not list.

`examples/gpu-node/observed/` is a hand-written sample, marked `"synthetic": true`. It proves the
parsers work on data shaped the way I expect, not on a real BMC.
