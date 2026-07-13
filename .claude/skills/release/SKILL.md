---
name: release
description: Release a new version of Solar Load Manager and deploy it to Home Assistant — bump manifest version, commit, push, create GitHub release, install via HACS, restart HA, verify. Use when the user asks to release, deploy, ship, or "bump and install" the integration.
---

# Release & deploy Solar Load Manager

Releases the integration and rolls it out to the live Home Assistant instance.
HACS installs from **GitHub releases**, not from pushed commits — a release tag
is mandatory or HA will keep seeing the old version.

## Inputs

- Version bump size: default **minor** (e.g. 1.8.0 → 1.9.0); use patch for
  fixes-only, or whatever the user specifies.
- Everything intended for the release must already be committed or staged in
  the working tree.

## Steps

1. **Run tests first** — abort the release if anything fails:
   ```bash
   rtk test python3 -m pytest tests/ -q
   ```

2. **Bump the version** in
   `custom_components/solar_load_manager/manifest.json` (`"version"` field).
   Current version is whatever is in the file; compute the new one from the
   bump size.

3. **Commit and push** (include the version bump; use a message describing the
   feature, not just "bump"):
   ```bash
   rtk git add -A && rtk git commit -m "<message>" && rtk git push
   ```

4. **Create the GitHub release** (tag format `vX.Y.Z`, title
   `vX.Y.Z — <short feature summary>`, notes = 1–3 sentence changelog):
   ```bash
   rtk gh release create vX.Y.Z -R porwisz/solar-load-manager -t "vX.Y.Z — <summary>" -n "<notes>"
   ```

5. **Install in HA via HACS** (Home Assistant MCP). The HACS repository id for
   this integration is `1287236572` (the `owner/repo` form fails on this
   server — use the numeric id):
   - `ha_manage_hacs(action="download", repository_id="1287236572", version="vX.Y.Z")`

6. **Restart Home Assistant**:
   - `ha_restart(confirm=True)`
   - The connection drops during restart — this is expected.

7. **Wait for HA to come back** (poll in a background shell, ~1–3 min):
   ```bash
   until curl -s -o /dev/null --max-time 3 http://homeassistant.local:8123/; do sleep 5; done; echo up
   ```

8. **Verify** the deployment:
   - `ha_search` for an entity affected by the release (or any
     `switch.slm_*` / `sensor.slm_*` entity) and confirm it exists and has a
     sane state.
   - Optionally `ha_get_hacs_info(action="search", query="solar load manager")`
     to confirm `installed_version` == new version.

9. **Report**: new version, release URL, and what was verified.

## Notes

- Entity ids are prefixed twice (`switch.slm_jacuzzi_jacuzzi_...`) because HA
  prepends the device name; this is the existing convention, don't fix it.
- If the HACS download reports the version as unavailable, the release may not
  have propagated yet — wait ~30 s and retry once.
