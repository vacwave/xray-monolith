#include "stdafx.h"
#include "r4_dlss.h"

#include <nvapi.h>
#include <dlss/include/nvsdk_ngx.h>
#include <dlss/include/nvsdk_ngx_helpers.h>

#pragma comment(lib, "version.lib")

namespace dlss
{
    namespace
    {
        // NGX project id for NVSDK_NGX_ENGINE_TYPE_CUSTOM (any fixed GUID)
        constexpr const char *project_id = "c1a8854c-b8a9-4c6f-9d3d-4d0a2f152cad";

        ID3D11Device *ngx_device = nullptr;
        NVSDK_NGX_Parameter *caps = nullptr;
        bool sr_available = false;
        u32 width = 0, height = 0;

        void exe_dir(wchar_t (&dir)[MAX_PATH])
        {
            GetModuleFileNameW(nullptr, dir, MAX_PATH);
            if (wchar_t *slash = wcsrchr(dir, L'\\'))
                *slash = 0;
        }

        // "driver 581.57, nvngx_dlss.dll 310.9.16.0"
        void versions(string256 &out)
        {
            string32 driver = "?";
            NvU32 ver = 0;
            NvAPI_ShortString branch;
            if (NvAPI_SYS_GetDriverAndBranchVersion(&ver, branch) == NVAPI_OK)
                xr_sprintf(driver, "%u.%02u", ver / 100, ver % 100);

            string64 dll = "missing";
            wchar_t path[MAX_PATH];
            exe_dir(path);
            wcscat_s(path, L"\\nvngx_dlss.dll");
            if (DWORD size = GetFileVersionInfoSizeW(path, nullptr))
            {
                xr_vector<u8> data(size);
                VS_FIXEDFILEINFO *info = nullptr;
                UINT len = 0;
                if (GetFileVersionInfoW(path, 0, size, data.data()) && VerQueryValueW(data.data(), L"\\", (void **)&info, &len))
                    xr_sprintf(dll, "%u.%u.%u.%u", HIWORD(info->dwFileVersionMS), LOWORD(info->dwFileVersionMS),
                               HIWORD(info->dwFileVersionLS), LOWORD(info->dwFileVersionLS));
            }
            xr_sprintf(out, "driver %s, nvngx_dlss.dll %s", driver, dll);
        }

        void log_render_sizes()
        {
            struct Mode
            {
                const char *name;
                NVSDK_NGX_PerfQuality_Value value;
            };
            const Mode modes[] = {
                {"DLAA", NVSDK_NGX_PerfQuality_Value_DLAA},
                {"Quality", NVSDK_NGX_PerfQuality_Value_MaxQuality},
                {"Balanced", NVSDK_NGX_PerfQuality_Value_Balanced},
                {"Performance", NVSDK_NGX_PerfQuality_Value_MaxPerf},
                {"Ultra Performance", NVSDK_NGX_PerfQuality_Value_UltraPerformance},
            };
            string512 sizes = "";
            for (const Mode &m : modes)
            {
                unsigned rw = 0, rh = 0, max_w, max_h, min_w, min_h;
                float sharpness;
                string64 item;
                if (NVSDK_NGX_SUCCEED(NGX_DLSS_GET_OPTIMAL_SETTINGS(caps, width, height, m.value, &rw, &rh, &max_w, &max_h,
                                                                    &min_w, &min_h, &sharpness)) &&
                    rw && rh)
                    xr_sprintf(item, "%s%s %ux%u", sizes[0] ? ", " : "", m.name, rw, rh);
                else
                    xr_sprintf(item, "%s%s n/a", sizes[0] ? ", " : "", m.name);
                xr_strcat(sizes, item);
            }
            Msg("* DLSS: render sizes for output %ux%u: %s", width, height, sizes);
        }
    } // namespace

    void create(ID3D11Device *device, u32 out_width, u32 out_height)
    {
        width = out_width;
        height = out_height;
        string256 ver;
        versions(ver);

        // NGX writes its own logs to the app data path
        string_path logs;
        FS.update_path(logs, "$logs$", "");
        wchar_t wlogs[MAX_PATH];
        MultiByteToWideChar(CP_ACP, 0, logs, -1, wlogs, MAX_PATH);
        wchar_t dir[MAX_PATH];
        exe_dir(dir);
        const wchar_t *paths[] = {dir};
        NVSDK_NGX_FeatureCommonInfo info = {};
        info.PathListInfo.Path = paths;
        info.PathListInfo.Length = 1;

        NVSDK_NGX_Result res = NVSDK_NGX_D3D11_Init_with_ProjectID(project_id, NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.5.3", wlogs,
                                                                   device, &info);
        if (NVSDK_NGX_FAILED(res))
        {
            Msg("* DLSS: unavailable (NGX init failed: %ls, 0x%08x; %s)", GetNGXResultAsString(res), res, ver);
            return;
        }
        ngx_device = device;

        res = NVSDK_NGX_D3D11_GetCapabilityParameters(&caps);
        if (NVSDK_NGX_FAILED(res))
        {
            Msg("* DLSS: unavailable (NGX capability query failed: %ls, 0x%08x; %s)", GetNGXResultAsString(res), res, ver);
            return;
        }

        int available = 0, needs_driver = 0, init_result = 0;
        unsigned min_major = 0, min_minor = 0;
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_SuperSampling_Available, &available);
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_SuperSampling_NeedsUpdatedDriver, &needs_driver);
        NVSDK_NGX_Parameter_GetUI(caps, NVSDK_NGX_Parameter_SuperSampling_MinDriverVersionMajor, &min_major);
        NVSDK_NGX_Parameter_GetUI(caps, NVSDK_NGX_Parameter_SuperSampling_MinDriverVersionMinor, &min_minor);
        NVSDK_NGX_Parameter_GetI(caps, NVSDK_NGX_Parameter_SuperSampling_FeatureInitResult, &init_result);
        if (!available)
        {
            if (needs_driver)
                Msg("* DLSS: unavailable (driver too old, needs %u.%02u; %s)", min_major, min_minor, ver);
            else
                Msg("* DLSS: unavailable (Super Resolution not supported: %ls, 0x%08x; %s)",
                    GetNGXResultAsString((NVSDK_NGX_Result)init_result), init_result, ver);
            return;
        }
        sr_available = true;
        Msg("* DLSS: available (%s)", ver);
        log_render_sizes();
    }

    void resize(u32 out_width, u32 out_height)
    {
        if (!sr_available || (out_width == width && out_height == height))
            return;
        width = out_width;
        height = out_height;
        log_render_sizes();
    }

    void destroy()
    {
        if (!ngx_device)
            return;
        if (caps)
            NVSDK_NGX_D3D11_DestroyParameters(caps);
        NVSDK_NGX_D3D11_Shutdown1(ngx_device);
        caps = nullptr;
        ngx_device = nullptr;
        sr_available = false;
        Msg("* DLSS: NGX shut down");
    }

    bool available()
    {
        return sr_available;
    }
} // namespace dlss
