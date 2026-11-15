#include "stdafx.h"
#include "xrHiddenMode.h"

extern bool shared_str_initialized;

bool hidden_mode()
{
    // read from the process command line: needed before Core params are parsed (splash window)
    static const bool enabled = []
    {
        const char *p = strstr(GetCommandLineA(), " -hidden");
        return p && (p[8] == 0 || p[8] == ' ');
    }();
    return enabled;
}

void hidden_mode_exit(unsigned code)
{
    if (!hidden_mode())
        return;
    if (shared_str_initialized)
    {
        xrLogger::SetImmediateMode(true);
        xrLogger::CloseLog();
    }
    TerminateProcess(GetCurrentProcess(), code);
}
