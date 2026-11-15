#include "stdafx.h"
#include "hidden_mode.h"
#include "XR_IOConsole.h"

HWND hidden_mode_create_window(HINSTANCE instance, const char *wndclass)
{
    return CreateWindowEx(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, wndclass, "S.T.A.L.K.E.R.: Anomaly (hidden)", WS_POPUP, 0,
                          0, 1920, 1080, 0L, 0, instance, 0L);
}

void hidden_mode_vid_mode(u32 &width, u32 &height)
{
    width = 1920;
    height = 1080;
}

namespace
{
    class HiddenCmdFile : public pureFrame
    {
    public:
        string_path path = "";
        u32 next_check = 0;

        void OnFrame() override
        {
            if (Device.dwTimeGlobal < next_check)
                return;
            next_check = Device.dwTimeGlobal + 250;

            FILE *f = fopen(path, "rb");
            if (!f)
                return;
            xr_vector<xr_string> commands;
            char line[1024];
            while (fgets(line, sizeof(line), f))
            {
                line[strcspn(line, "\r\n")] = 0;
                if (line[0])
                    commands.push_back(line);
            }
            fclose(f);
            DeleteFile(path);

            for (const xr_string &cmd : commands)
            {
                Msg("hidden_cmd: %s", cmd.c_str());
                Console->Execute(cmd.c_str());
            }
        }
    };
    HiddenCmdFile cmd_file;
} // namespace

void hidden_mode_start()
{
    // the window is never activated, so WM_ACTIVATE would leave the engine paused
    Device.b_is_Active = TRUE;
    const char *p = strstr(GetCommandLine(), "-hidden_cmd ");
    if (!p || sscanf(p + xr_strlen("-hidden_cmd "), "%[^ ]", cmd_file.path) != 1)
        return;
    Msg("hidden mode: console commands from %s", cmd_file.path);
    Device.seqFrame.Add(&cmd_file, REG_PRIORITY_LOW);
}
