#include "stdafx.h"
#include "r4_dlss.h"

#include <DLSS/nvsdk_ngx.h>
#include <DLSS/nvsdk_ngx_helpers.h>

#pragma comment(lib, "DLSS/nvsdk_ngx_d.lib")

CDlss Dlss;

// NGX without an NVIDIA application id: project-id GUID + custom engine type (sdk.md)
static const char* dlss_project_id = "6c1f3e2a-8b4d-4f7a-9e05-3d2c7a91b4e8";

static LPCSTR mode_name(u32 mode)
{
	static LPCSTR names[] = {"off", "dlaa", "quality", "balanced", "performance", "ultra_performance"};
	return mode < sizeof(names) / sizeof(names[0]) ? names[mode] : "?";
}

static NVSDK_NGX_PerfQuality_Value perf_quality(u32 mode)
{
	switch (mode)
	{
	case CDlss::MODE_DLAA: return NVSDK_NGX_PerfQuality_Value_DLAA;
	case CDlss::MODE_QUALITY: return NVSDK_NGX_PerfQuality_Value_MaxQuality;
	case CDlss::MODE_BALANCED: return NVSDK_NGX_PerfQuality_Value_Balanced;
	case CDlss::MODE_PERFORMANCE: return NVSDK_NGX_PerfQuality_Value_MaxPerf;
	default: return NVSDK_NGX_PerfQuality_Value_UltraPerformance;
	}
}

// NGX log (any thread): only the DLSS snippet version and the preset NGX really uses (shows driver / NVIDIA app overrides)
static void NVSDK_CONV ngx_log(const char* message, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature)
{
	if (!message || !(strstr(message, "snippet") || (strstr(message, "NgxDltss") && strstr(message, "Preset"))))
		return;
	string1024 line;
	xr_strcpy(line, message);
	for (size_t n = xr_strlen(line); n && (line[n - 1] == '\n' || line[n - 1] == '\r'); --n)
		line[n - 1] = 0;
	Msg("* NGX: %s", line);
}

bool CDlss::init()
{
	if (params)
		return true;
	if (init_tried)
		return false;
	init_tried = true;

	if (HW.Caps.id_vendor != 0x10DE)
	{
		Msg("! DLSS unavailable: not an NVIDIA GPU");
		return false;
	}

	// NGX loads the DLSS model from the exe folder
	string_path dll;
	GetModuleFileNameA(nullptr, dll, sizeof(dll));
	if (LPSTR slash = strrchr(dll, '\\'))
		slash[1] = 0;
	xr_strcat(dll, "nvngx_dlss.dll");
	if (GetFileAttributesA(dll) == INVALID_FILE_ATTRIBUTES)
	{
		Msg("! DLSS unavailable: %s is missing", dll);
		return false;
	}

	// NGX writes its own logs there
	string_path logs;
	FS.update_path(logs, "$logs$", "");
	wchar_t wlogs[_MAX_PATH];
	MultiByteToWideChar(CP_ACP, 0, logs, -1, wlogs, _MAX_PATH);

	NVSDK_NGX_FeatureCommonInfo info = {};
	info.LoggingInfo.LoggingCallback = ngx_log;
	info.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_ON;
	NVSDK_NGX_Result res = NVSDK_NGX_D3D11_Init_with_ProjectID(dlss_project_id, NVSDK_NGX_ENGINE_TYPE_CUSTOM, "1.0", wlogs, HW.pDevice, &info);
	if (NVSDK_NGX_FAILED(res))
	{
		Msg("! DLSS unavailable: NGX init failed (0x%08x)%s", res, res == NVSDK_NGX_Result_FAIL_FeatureNotSupported ? ", GPU or driver not supported" : "");
		return false;
	}
	ngx_initialized = true;

	res = NVSDK_NGX_D3D11_GetCapabilityParameters(&params);
	if (NVSDK_NGX_FAILED(res) || !params)
	{
		Msg("! DLSS unavailable: NGX capability parameters failed (0x%08x)", res);
		params = nullptr;
		shutdown();
		init_tried = true;
		return false;
	}

	int available = 0;
	NVSDK_NGX_Parameter_GetI(params, NVSDK_NGX_Parameter_SuperSampling_Available, &available);
	if (!available)
	{
		int needs_driver = 0, init_result = 0;
		unsigned int major = 0, minor = 0;
		NVSDK_NGX_Parameter_GetI(params, NVSDK_NGX_Parameter_SuperSampling_NeedsUpdatedDriver, &needs_driver);
		NVSDK_NGX_Parameter_GetUI(params, NVSDK_NGX_Parameter_SuperSampling_MinDriverVersionMajor, &major);
		NVSDK_NGX_Parameter_GetUI(params, NVSDK_NGX_Parameter_SuperSampling_MinDriverVersionMinor, &minor);
		NVSDK_NGX_Parameter_GetI(params, NVSDK_NGX_Parameter_SuperSampling_FeatureInitResult, &init_result);
		if (needs_driver)
			Msg("! DLSS unavailable: driver too old, needs %u.%u or newer", major, minor);
		else
			Msg("! DLSS unavailable: not supported on this GPU (feature init result 0x%08x)", init_result);
		shutdown();
		init_tried = true;
		return false;
	}

	Msg("* DLSS: NGX initialised (DLSS SDK 310.9.16)");
	return true;
}

void CDlss::shutdown()
{
	release_feature();
	if (params)
		NVSDK_NGX_D3D11_DestroyParameters(params);
	params = nullptr;
	if (ngx_initialized)
		NVSDK_NGX_D3D11_Shutdown1(HW.pDevice);
	ngx_initialized = false;
	init_tried = false;
}

void CDlss::release_feature()
{
	if (feature)
		NVSDK_NGX_D3D11_ReleaseFeature(feature);
	feature = nullptr;
	_RELEASE(exposure);
	create_failed = false;
	eval_failures = 0;
}

// "default" = M on RTX 20/30 (device ids below Ada; K gives white / dark-blurred frames there, sdk.md), the driver's choice on newer GPUs
u32 CDlss::preset_hint() const
{
	switch (ps_r4_dlss_preset)
	{
	case PRESET_K: return NVSDK_NGX_DLSS_Hint_Render_Preset_K;
	case PRESET_M: return NVSDK_NGX_DLSS_Hint_Render_Preset_M;
	case PRESET_L: return NVSDK_NGX_DLSS_Hint_Render_Preset_L;
	default: return HW.Caps.id_device < 0x2600 ? NVSDK_NGX_DLSS_Hint_Render_Preset_M : NVSDK_NGX_DLSS_Hint_Render_Preset_Default;
	}
}

bool CDlss::render_size(u32 out_w, u32 out_h, u32& w, u32& h)
{
	if (!init())
		return false;

	feature_mode = ps_r4_dlss;
	unsigned int opt_w = 0, opt_h = 0, max_w = 0, max_h = 0, min_w = 0, min_h = 0;
	float sharpness = 0.f;
	NVSDK_NGX_Result res = NGX_DLSS_GET_OPTIMAL_SETTINGS(params, out_w, out_h, perf_quality(feature_mode), &opt_w, &opt_h, &max_w, &max_h, &min_w, &min_h, &sharpness);
	if (NVSDK_NGX_FAILED(res) || !opt_w || !opt_h)
	{
		Msg("! DLSS unavailable: no render size for mode %s at %ux%u (0x%08x)", mode_name(feature_mode), out_w, out_h, res);
		return false;
	}
	w = opt_w;
	h = opt_h;
	return true;
}

bool CDlss::create_feature(u32 render_w, u32 render_h, u32 out_w, u32 out_h)
{
	const u32 preset = preset_hint();
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_DLAA, preset);
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_Quality, preset);
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_Balanced, preset);
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_Performance, preset);
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_UltraPerformance, preset);
	NVSDK_NGX_Parameter_SetUI(params, NVSDK_NGX_Parameter_DLSS_Hint_Render_Preset_UltraQuality, preset);

	// Preset K reads an exposure even for LDR colour: missing or auto exposure gave white/purple frames, so pass 1.0
	const float one = 1.f;
	D3D11_TEXTURE2D_DESC desc = {};
	desc.Width = desc.Height = desc.MipLevels = desc.ArraySize = 1;
	desc.Format = DXGI_FORMAT_R32_FLOAT;
	desc.SampleDesc.Count = 1;
	desc.Usage = D3D11_USAGE_IMMUTABLE;
	desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
	D3D11_SUBRESOURCE_DATA data = {&one, sizeof(one), 0};
	if (FAILED(HW.pDevice->CreateTexture2D(&desc, &data, &exposure)))
		exposure = nullptr;

	// LDR colour (no IsHDR), motion vectors at render size without jitter, depth 0 near .. 1 far
	NVSDK_NGX_DLSS_Create_Params create = {};
	create.Feature.InWidth = render_w;
	create.Feature.InHeight = render_h;
	create.Feature.InTargetWidth = out_w;
	create.Feature.InTargetHeight = out_h;
	create.Feature.InPerfQualityValue = perf_quality(feature_mode);
	create.InFeatureCreateFlags = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes;

	NVSDK_NGX_Result res = NGX_D3D11_CREATE_DLSS_EXT(HW.pContext, &feature, params, &create);
	if (NVSDK_NGX_FAILED(res) || !feature)
	{
		Msg("! DLSS feature creation failed (0x%08x): bilinear upscale until vid_restart", res);
		feature = nullptr;
		create_failed = true;
		return false;
	}

	static LPCSTR preset_names[] = {"default", "K", "M", "L"};
	unsigned long long vram = 0;
	NGX_DLSS_GET_STATS(params, &vram);
	Msg("* DLSS %s: render %ux%u -> output %ux%u, preset %s (hint %u), VRAM %llu MB", mode_name(feature_mode), render_w, render_h, out_w, out_h,
		preset_names[ps_r4_dlss_preset < 4 ? ps_r4_dlss_preset : 0], preset, vram >> 20);
	feature_w = render_w;
	feature_h = render_h;
	feature_out_w = out_w;
	feature_out_h = out_h;
	return true;
}

bool CDlss::evaluate(ID3D11Resource* color, ID3D11Resource* output, ID3D11Resource* depth, ID3D11Resource* mv,
                     u32 render_w, u32 render_h, u32 out_w, u32 out_h, const Fvector2& jitter, bool reset)
{
	if (!params || create_failed)
		return false;
	if (feature && (feature_w != render_w || feature_h != render_h || feature_out_w != out_w || feature_out_h != out_h))
		release_feature();
	if (!feature && !create_feature(render_w, render_h, out_w, out_h))
		return false;

	NVSDK_NGX_D3D11_DLSS_Eval_Params eval = {};
	eval.Feature.pInColor = color;
	eval.Feature.pInOutput = output;
	eval.pInDepth = depth;
	eval.pInMotionVectors = mv;
	// Same direction as the motion vectors: screen offset of the geometry in render pixels
	eval.InJitterOffsetX = jitter.x;
	eval.InJitterOffsetY = jitter.y;
	eval.InRenderSubrectDimensions.Width = render_w;
	eval.InRenderSubrectDimensions.Height = render_h;
	eval.InReset = reset ? 1 : 0;
	eval.InMVScaleX = 1.f;
	eval.InMVScaleY = 1.f;
	eval.pInExposureTexture = exposure;

	NVSDK_NGX_Result res = NGX_D3D11_EVALUATE_DLSS_EXT(HW.pContext, feature, params, &eval);
	if (NVSDK_NGX_FAILED(res))
	{
		// Logged once; repeated failures drop to the bilinear path until the next vid_restart
		if (!eval_failures)
			Msg("! DLSS evaluate failed (0x%08x): bilinear upscale this frame", res);
		if (++eval_failures >= 30)
		{
			Msg("! DLSS evaluate keeps failing: bilinear upscale until vid_restart");
			create_failed = true;
		}
		return false;
	}
	eval_failures = 0;
	return true;
}
