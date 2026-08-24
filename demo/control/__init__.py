"""demo/control — the partner-demo control UI backend (demo/SPEC.md).

Not part of the QueryGate product: nothing under src/querygate/ imports this
package, and demo/ is excluded from the wheel, sdist, and container image
(see demo/SPEC.md's "Hard safety constraints"). This __init__.py exists only
so demo/control/*.py can use ordinary intra-package relative imports. Run the
app via run.sh (`poetry run uvicorn demo.control.app:app --host 127.0.0.1
--port 8900`, invoked from the repo root) — app.py has no __main__ guard, so
`python -m demo.control.app` alone will import and exit without starting a
server.
"""
