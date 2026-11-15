#pragma once

// -hidden: no visible window, no focus/cursor/display takeover, no error dialogs (unattended smoke checks)
XRCORE_API bool hidden_mode();
// In -hidden mode: drain the log and exit with code instead of showing a dialog; no-op otherwise
XRCORE_API void hidden_mode_exit(unsigned code);
