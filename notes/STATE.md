# STATE
Scope: MT only (`dev-mt-mcp`; non-MT dropped). Agent ⇄ Python MCP supervisor (`mcp/anomaly_mcp.py`, server `anomaly` via `.mcp.json`) ⇄ TCP JSON-lines ⇄ engine (`-mcp_port`).

Done (all pushed): 0 notes · 1 IPC server 757d973c · 2 multi-instance 5ac6faea · 3 log streaming 6045689a · 4 silent runs 8795fb14 · 5 console/lua exec ca66329c · 6 supervisor dd980ff1 · 7 crash capture + hang detection + WER dumps 08b0cd0e.

Queue: empty — plan next goals in a fresh session.
Candidates: game state queries; input/movement (`level.press_action`, mouse-look in C++); screenshots (`ScreenshotAsyncBegin/End`); PyInstaller single exe.

Setup: `py -3.11 -m pip install "mcp>=2.3,<3"`; WER LocalDumps installed via `mcp/setup_wer.ps1` (admin, done 2026-11-15).
Caps 3 vanilla / 2 GAMMA. Vanilla ≈4.1 GB RAM / 1.6 GB VRAM. Template user.ltx has r__framelimit 30 (uncap for FPS tests).
Open issues: loading saves `1`/`bench_g1` crashes in `CALifeSimulatorBase::register_object` (save/mod mismatch, also with -showwindow); one unreproduced startup hang before logs dir (launch timeout now auto-dumps); long level-load threshold untested on a multi-minute load.
Testing: new game via `game_launch(new_game=true)`; 180 s timeouts; kill leftover AnomalyDX11AVX after runs.
