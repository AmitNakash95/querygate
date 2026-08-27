# Third-party licences

**Generated file — do not edit by hand.** Regenerate with `make license-report`; `make license-check` fails if it drifts from `poetry.lock` or if any dependency carries a licence that is not permissive and not individually recorded. The check runs in the unit suite (`tests/unit/test_third_party_licenses.py`) and in `make release-check`.

> ⚠️ **5 non-permissive dependencies are recorded but not yet confirmed by a human, 1 of them in the redistributed set.** The gate passes on those records; that is not the same as the question being settled. See the review section below.

Source of truth: `poetry.lock` — **138 Python packages**, of which **60** are in the `main` group and **78** are not redistributed — development, test, and CI tooling.

Of those 60, **56** install into the published Linux image; 4 carry an environment marker that excludes them there (`async-timeout`, `colorama`, `pywin32`, `win32-setctime`). That is why the CycloneDX SBOM in `dist/`, which lists only what installs, has 56 third-party components where this report has 60 rows. This report deliberately keeps them: a Windows-only wheel in the `main` group is still installed for a Windows operator, and its licence still counts.

**What “redistributed” means here.** The `main` group is what the published container image contains: `Dockerfile` builds it with `poetry install --no-root --only main`, minus any package whose environment marker excludes it on the image's platform. The wheel and source distribution contain none of these packages: QueryGate's wheel declares only its **direct** requirements, and `pip` resolves the rest transitively, so a `pip install querygate` fetches them from PyPI — subject to the same environment markers, and to pip's own resolution against the declared version ranges rather than to this lockfile's pins. `certifi` is one of the transitive ones — it is not named in QueryGate's own metadata at all. The `dev` group is neither shipped nor fetched by a consumer.

**Scope limit.** This inventory covers Python packages in `poetry.lock` only. The container image additionally layers a Debian `bookworm` userland and Microsoft's `msodbcsql18` ODBC driver (installed under `ACCEPT_EULA=Y`, its own proprietary terms). Those are not Python packages and are not in `poetry.lock`, so this gate does not see them — they are assessed separately in `docs/CONTAINER_IMAGE_LICENCES.md`.

Licence identifiers are read from each package's own metadata in this order: `License-Expression` (authoritative under PEP 639), then Trove classifiers, then the free-text `License` field — except that a classifier naming only a licence *family* defers to a more specific recognised free-text value. PEP 639 ranks only the first of those; it deprecates the other two without ordering them, so the rest is this tool's own choice, made because some packages put their entire licence body or the string `UNKNOWN` in the free-text field.

This report and the CycloneDX SBOM in `dist/` can name the same licence differently, because they map the same declared metadata through different tables. Neither is authoritative on its own — the package's own bundled licence text is. Where they differ, this report's identifier and the evidence behind it are stated here.

## Summary

| Licence | Packages |
| --- | --- |
| Apache-2.0 | 41 |
| Apache-2.0 AND BSD-2-Clause | 1 |
| Apache-2.0 OR BSD | 2 |
| Apache-2.0 OR BSD-3-Clause | 1 |
| BSD | 2 |
| BSD-2-Clause | 3 |
| BSD-3-Clause | 18 |
| ISC | 1 |
| LGPL-2.0-or-later | 1 |
| MIT | 59 |
| MIT-0 | 2 |
| MPL-2.0 | 4 |
| PSF-2.0 | 3 |

No GPL or AGPL licence is a locked package, in either group — both GPL MySQL drivers appear in `poetry.lock` only inside SQLAlchemy's unselected `extras`, which Poetry never resolves. Strong copyleft is blocking in every group and cannot be waived by a reviewed entry.

## Non-permissive licences — recorded, review pending

Every entry below is a weak-copyleft licence that the deny-by-default gate refuses unless a record exists in `security/copyleft-license-allowlist.json`. Each record names the package, the licence, whether QueryGate redistributes it, and why it is acceptable. **A record marked `draft` is drafted analysis awaiting owner and counsel confirmation. It is not legal advice, it is not settled, and the gate passing over it is not evidence that the question is closed.**

### `certifi` — MPL-2.0 (draft)

- **Scope:** redistributed with QueryGate.
- **Required by:** `httpcore`, `httpx`, `requests`, `snowflake-connector-python`; QueryGate does not declare it directly.
- **Imported by QueryGate source:** no.
- **Reason** (drafted legal reading — the three facts above are machine-checked on every run, this is not): DRAFTED ANALYSIS, NOT A LEGAL OPINION — owner and counsel confirmation still required. The question is now sharper, not softer: QueryGate ships PROPRIETARY and closed-source (2026-08-23), so a reciprocal licence in the redistributed set is a harder problem than it was under the cancelled source-available plan, not an easier one. THE ONLY NON-PERMISSIVE LICENCE IN THE REDISTRIBUTED SET. certifi is the Mozilla CA root bundle; of the requirers listed above, httpx/httpcore/requests are what carry it into the `main` group, while snowflake-connector-python is dev-only. Redistribution surface, stated precisely, because this is the premise of the question: the published container image contains it (`Dockerfile` runs `poetry install --no-root --only main`). The wheel and sdist contain no dependencies at all, and QueryGate's wheel does not even declare certifi — it is not among the `Requires-Dist` entries in the built wheel's METADATA (verified 2026-08-21 against `dist/querygate-0.1.0-py3-none-any.whl`: 24 direct requirements, certifi not one of them); `pip` would resolve it transitively, but the wheel is NOT published — the container image is the only distribution channel (docs/RELEASING.md, docs/business/GTM_SAAS.md §6; signing and SLSA provenance are wired in release.yml but have not yet run on a tag), so the image is the whole redistribution surface. MPL-2.0 is file-level (weak) copyleft: §3.3 permits distributing a Larger Work under other terms provided the MPL-covered files stay under MPL-2.0 and their source stays available. QueryGate ships certifi verbatim as its own installed package, unmodified and not derived from — httpx/httpcore/requests load it only for the default CA bundle — so no QueryGate source file becomes MPL-covered. This is the one dependency-licence question that must be put to counsel alongside the EULA (docs/legal/EULA.en.md).
- **Recorded:** 2026-08-21

### `chardet` — LGPL-2.0-or-later (draft)

- **Scope:** development/CI only — never redistributed.
- **Required by:** `cyclonedx-bom`; QueryGate does not declare it directly.
- **Imported by QueryGate source:** no.
- **Reason** (drafted legal reading — the three facts above are machine-checked on every run, this is not): DRAFTED ANALYSIS, NOT A LEGAL OPINION — proposed as not requiring a counsel question; owner to confirm that routing as well as the substance. The only LGPL (weak, library-level copyleft) licence anywhere in poetry.lock, and it is NOT redistributed. On the identifier: chardet declares the Trove classifier `GNU Lesser General Public License v2 or later (LGPLv2+)`, which this gate maps to LGPL-2.0-or-later, so that is what is recorded; its bundled LICENSE file is in fact `GNU LESSER GENERAL PUBLIC LICENSE Version 2.1`. Same tier either way — the record deliberately reports the declared classifier rather than a point version the metadata does not state. `cyclonedx-bom`, the requirer above, is the dev-group tool `scripts/generate_sbom.py` uses to build the SBOM. chardet is absent from the `main` group, so it is absent from the container image and from the wheel's and sdist's declared requirements. LGPL obligations attach on distribution of the library; QueryGate distributes neither chardet nor anything derived from it. Re-review immediately if it ever enters the `main` group — the gate fails automatically in that case, because this entry records `redistributed: false`.
- **Recorded:** 2026-08-21

### `fqdn` — MPL-2.0 (draft)

- **Scope:** development/CI only — never redistributed.
- **Required by:** `jsonschema` (optional extra); QueryGate does not declare it directly.
- **Imported by QueryGate source:** no.
- **Reason** (drafted legal reading — the three facts above are machine-checked on every run, this is not): DRAFTED ANALYSIS, NOT A LEGAL OPINION — proposed as not requiring a counsel question; owner to confirm. Dev-only and not redistributed: absent from the `main` group, so absent from the container image and from the wheel's and sdist's declared requirements. It reaches the lock through `jsonschema[format-nongpl]`, which `cyclonedx-python-lib` requires for its validation extra — which is why the requirer above is jsonschema, the package that declares the optional dependency, rather than cyclonedx-python-lib, which selects the extra. Note that plain `jsonschema` IS in the `main` group (via `mcp`), but without the `format-nongpl` extra, which is why fqdn resolves to `dev` alone. MPL-2.0 is file-level (weak) copyleft whose obligations run with distribution of the covered files; QueryGate distributes none of them. Stated in full rather than by reference to the certifi record, so a correction there cannot silently propagate here.
- **Recorded:** 2026-08-21

### `hypothesis` — MPL-2.0 (draft)

- **Scope:** development/CI only — never redistributed.
- **Required by:** nothing else in the lockfile — and declared directly by QueryGate.
- **Imported by QueryGate source:** no.
- **Reason** (drafted legal reading — the three facts above are machine-checked on every run, this is not): DRAFTED ANALYSIS, NOT A LEGAL OPINION — proposed as not requiring a counsel question; owner to confirm. Dev-only and not redistributed. The only record here that QueryGate declares directly, in `[tool.poetry.group.dev.dependencies]` — nothing else in the lock depends on it. It is imported by the property-based tests in `tests/unit/test_compiler_properties.py`, the only module in the repository that imports it; note that is a test module, not `src/querygate/`, which is what `imported_by_querygate_source` tracks. MPL-2.0 is file-level (weak) copyleft whose obligations run with distribution of the covered files; a test-time tool that never enters the `main` group is never distributed.
- **Recorded:** 2026-08-21

### `pathspec` — MPL-2.0 (draft)

- **Scope:** development/CI only — never redistributed.
- **Required by:** `black`; QueryGate does not declare it directly.
- **Imported by QueryGate source:** no.
- **Reason** (drafted legal reading — the three facts above are machine-checked on every run, this is not): DRAFTED ANALYSIS, NOT A LEGAL OPINION — proposed as not requiring a counsel question; owner to confirm. Dev-only and not redistributed. Its sole requirer, `black`, is the formatter. MPL-2.0 is file-level (weak) copyleft whose obligations run with distribution of the covered files; a build-time tool that never enters the `main` group is never distributed.
- **Recorded:** 2026-08-21

## MySQL driver note

MySQL client libraries are a well-known copyleft trap: `mysqlclient` and `mysql-connector-python` are both GPL-licensed, and neither is a locked package here (both appear only inside SQLAlchemy's unselected `extras`). QueryGate's MySQL support uses **`asyncmy`**, which this pass resolves to **Apache-2.0** from its own declared `Apache-2.0`.

## Redistributed with QueryGate (`main` group)

| Package | Version | Licence |
| --- | --- | --- |
| `aioodbc` | 0.5.0 | Apache-2.0 |
| `annotated-doc` | 0.0.4 | MIT |
| `annotated-types` | 0.7.0 | MIT |
| `anyio` | 4.14.2 | MIT |
| `async-timeout` | 5.0.1 | Apache-2.0 ᵈ |
| `asyncmy` | 0.2.11 | Apache-2.0 |
| `asyncpg` | 0.30.0 | Apache-2.0 |
| `attrs` | 25.4.0 | MIT |
| `boto3` | 1.43.65 | Apache-2.0 |
| `botocore` | 1.43.65 | Apache-2.0 |
| `certifi` | 2026.6.17 | MPL-2.0 |
| `cffi` | 2.1.0 | MIT-0 |
| `charset-normalizer` | 3.4.9 | MIT |
| `click` | 8.4.2 | BSD-3-Clause |
| `colorama` | 0.4.6 | BSD ᵈ |
| `cryptography` | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| `defusedxml` | 0.7.1 | PSF-2.0 |
| `fastapi` | 0.139.2 | MIT |
| `greenlet` | 3.0.3 | MIT |
| `h11` | 0.16.0 | MIT |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpcore2` | 2.9.1 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `httpx2` | 2.9.1 | BSD-3-Clause |
| `hvac` | 2.4.0 | Apache-2.0 |
| `idna` | 3.18 | BSD-3-Clause |
| `jmespath` | 1.1.0 | MIT |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `loguru` | 0.7.3 | MIT |
| `mcp` | 2.0.0 | MIT |
| `mcp-types` | 2.0.0 | MIT |
| `opentelemetry-api` | 1.44.0 | Apache-2.0 |
| `prometheus-client` | 0.25.0 | Apache-2.0 AND BSD-2-Clause |
| `pycparser` | 3.0 | BSD-3-Clause |
| `pydantic` | 2.13.4 | MIT |
| `pydantic-core` | 2.46.4 | MIT |
| `pydantic-settings` | 2.9.1 | MIT |
| `pyjwt` | 2.13.0 | MIT |
| `pyodbc` | 5.2.0 | MIT-0 |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 OR BSD |
| `python-dotenv` | 1.2.2 | BSD-3-Clause |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `pywin32` | 312 | PSF-2.0 ᵈ |
| `pyyaml` | 6.0.3 | MIT |
| `redis` | 8.0.1 | MIT |
| `referencing` | 0.37.0 | MIT |
| `requests` | 2.34.2 | Apache-2.0 |
| `rpds-py` | 2026.6.3 | MIT |
| `s3transfer` | 0.19.2 | Apache-2.0 |
| `six` | 1.17.0 | MIT |
| `sqlalchemy` | 2.0.41 | MIT |
| `sse-starlette` | 3.0.3 | BSD-3-Clause |
| `starlette` | 1.3.1 | BSD-3-Clause |
| `truststore` | 0.10.4 | MIT |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.2 | MIT |
| `urllib3` | 2.7.0 | MIT |
| `uvicorn` | 0.51.0 | BSD-3-Clause |
| `win32-setctime` | 1.2.0 | MIT ᵈ |

## Not redistributed (development, test, and CI tooling)

| Package | Version | Licence |
| --- | --- | --- |
| `aiosqlite` | 0.22.1 | MIT |
| `arrow` | 1.4.0 | Apache-2.0 |
| `asn1crypto` | 1.5.1 | MIT |
| `bandit` | 1.9.4 | Apache-2.0 |
| `black` | 26.5.1 | MIT |
| `boolean-py` | 5.0 | BSD-2-Clause |
| `cachecontrol` | 0.14.4 | Apache-2.0 |
| `chardet` | 5.2.0 | LGPL-2.0-or-later |
| `coverage` | 7.15.2 | Apache-2.0 |
| `cyclonedx-bom` | 7.3.0 | Apache-2.0 |
| `cyclonedx-python-lib` | 11.11.0 | Apache-2.0 |
| `fakeredis` | 2.36.2 | BSD-3-Clause |
| `filelock` | 3.31.0 | MIT |
| `fqdn` | 1.5.1 | MPL-2.0 |
| `google-api-core` | 2.34.0 | Apache-2.0 |
| `google-auth` | 2.56.3 | Apache-2.0 |
| `google-cloud-bigquery` | 3.43.0 | Apache-2.0 |
| `google-cloud-core` | 2.6.1 | Apache-2.0 |
| `google-crc32c` | 1.8.0 | Apache-2.0 ᵈ |
| `google-resumable-media` | 2.10.1 | Apache-2.0 |
| `googleapis-common-protos` | 1.75.1 | Apache-2.0 |
| `grpcio` | 1.83.0 | Apache-2.0 |
| `grpcio-status` | 1.83.0 | Apache-2.0 |
| `hypothesis` | 6.157.0 | MPL-2.0 |
| `iniconfig` | 2.1.0 | MIT |
| `isoduration` | 20.11.0 | ISC |
| `jsonpointer` | 3.1.1 | BSD-3-Clause |
| `lark` | 1.3.1 | MIT |
| `license-expression` | 30.4.4 | Apache-2.0 |
| `lupa` | 2.8 | MIT |
| `lxml` | 6.1.1 | BSD-3-Clause |
| `markdown-it-py` | 4.2.0 | MIT |
| `markupsafe` | 3.0.3 | BSD-3-Clause |
| `mdurl` | 0.1.2 | MIT |
| `moto` | 5.2.2 | Apache-2.0 |
| `msgpack` | 1.2.1 | Apache-2.0 |
| `mypy-extensions` | 1.1.0 | MIT |
| `packageurl-python` | 0.17.6 | MIT |
| `packaging` | 25.0 | Apache-2.0 OR BSD |
| `pathspec` | 1.1.1 | MPL-2.0 |
| `pip` | 26.1.2 | MIT |
| `pip-api` | 0.0.34 | Apache-2.0 |
| `pip-audit` | 2.10.1 | Apache-2.0 |
| `pip-requirements-parser` | 32.0.1 | MIT |
| `platformdirs` | 4.3.8 | MIT |
| `pluggy` | 1.6.0 | MIT |
| `proto-plus` | 1.28.3 | Apache-2.0 |
| `protobuf` | 7.35.1 | BSD-3-Clause |
| `py-serializable` | 2.1.0 | Apache-2.0 |
| `pyasn1` | 0.6.4 | BSD-2-Clause |
| `pyasn1-modules` | 0.4.2 | BSD |
| `pygments` | 2.20.0 | BSD-2-Clause |
| `pyopenssl` | 26.4.0 | Apache-2.0 |
| `pyparsing` | 3.3.2 | MIT |
| `pytest` | 9.1.1 | MIT |
| `pytest-asyncio` | 1.4.0 | Apache-2.0 |
| `pytest-cov` | 7.1.0 | MIT |
| `pytest-mock` | 3.15.1 | MIT |
| `pytokens` | 0.4.1 | MIT |
| `pytz` | 2026.3.post1 | MIT |
| `responses` | 0.26.2 | Apache-2.0 |
| `rfc3339-validator` | 0.1.4 | MIT |
| `rfc3986-validator` | 0.1.1 | MIT |
| `rfc3987-syntax` | 1.1.0 | MIT |
| `rich` | 15.0.0 | MIT |
| `snowflake-connector-python` | 4.7.1 | Apache-2.0 |
| `snowflake-sqlalchemy` | 1.11.0 | Apache-2.0 |
| `sortedcontainers` | 2.4.0 | Apache-2.0 |
| `sqlalchemy-bigquery` | 1.17.2 | MIT |
| `stevedore` | 5.9.0 | Apache-2.0 |
| `tomli` | 2.4.1 | MIT |
| `tomli-w` | 1.2.0 | MIT |
| `tomlkit` | 0.15.1 | MIT |
| `tzdata` | 2026.3 | Apache-2.0 |
| `uri-template` | 1.3.0 | MIT |
| `webcolors` | 25.10.0 | BSD-3-Clause |
| `werkzeug` | 3.1.8 | BSD-3-Clause |
| `xmltodict` | 1.0.4 | MIT |

## ᵈ Licences not readable from a local install

These packages' licences are recorded from the evidence below rather than read from local metadata — either because the package cannot be installed here (a Windows-only wheel, or an environment marker that does not apply), or because it declares no licence metadata at all. Two things keep these records from becoming stale snapshots: when such a package *is* installed and does declare a licence, `make license-check` cross-checks the record against the real metadata and fails on a mismatch; and the nightly workflow re-reads every record straight from PyPI (`scripts/check_licenses.py --verify-overrides`) and fails if what upstream declares no longer matches.

| Package | Version | Licence | Why not readable locally | Evidence |
| --- | --- | --- | --- | --- |
| `async-timeout` | 5.0.1 | Apache-2.0 | Locked only under the marker `python_full_version < "3.11.3"`, so it is not installed on this project's interpreter and its metadata cannot be read locally. | PyPI metadata for async-timeout 5.0.1 declares `License :: OSI Approved :: Apache Software License` (`https://pypi.org/pypi/async-timeout/5.0.1/json`, read 2026-08-21). |
| `colorama` | 0.4.6 | BSD | Locked only under `platform_system == "Windows" or sys_platform == "win32"`, so it is not installed on Linux or macOS. | PyPI metadata for colorama 0.4.6 declares `License :: OSI Approved :: BSD License` (`https://pypi.org/pypi/colorama/0.4.6/json`, read 2026-08-21). The classifier is the generic BSD one, so this record does not claim a specific 2- or 3-clause variant. |
| `google-crc32c` | 1.8.0 | Apache-2.0 | Installed, but its wheel metadata declares no `License-Expression`, no `License ::` classifier, and no `License` field — only a `License-File` pointer. | The bundled `google_crc32c-1.8.0.dist-info/licenses/LICENSE` is the verbatim Apache License, Version 2.0 text (read from the installed distribution, 2026-08-21). |
| `pywin32` | 312 | PSF-2.0 | Locked only under `sys_platform == "win32"`, so it is not installed on Linux or macOS. | PyPI metadata for pywin32 312 declares `License :: OSI Approved :: Python Software Foundation License` and `License: PSF` (`https://pypi.org/pypi/pywin32/312/json`, read 2026-08-21). |
| `win32-setctime` | 1.2.0 | MIT | Locked only under `sys_platform == "win32"`, so it is not installed on Linux or macOS. | PyPI metadata for win32-setctime 1.2.0 declares `License :: OSI Approved :: MIT License` and `License: MIT license` (`https://pypi.org/pypi/win32-setctime/1.2.0/json`, read 2026-08-21). |

---

*This report states the licences the dependencies themselves declare. It is evidence for a licence review, not a legal opinion, and it says nothing about QueryGate's own licence.*
