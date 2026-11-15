#pragma once

// -hidden (see xrCore/xrHiddenMode.h): render window that is never shown or activated
HWND hidden_mode_create_window(HINSTANCE instance, const char *wndclass);
// -hidden: fixed back buffer size, independent of the user's vid_mode
void hidden_mode_vid_mode(u32 &width, u32 &height);
// -hidden_cmd <file>: from the main loop on, console commands in <file> (one per line) are run and the file is deleted
void hidden_mode_start();
