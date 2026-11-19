#pragma once

// NVIDIA DLSS Super Resolution through NGX on the D3D11 device (notes/dlss/sdk.md)
struct NVSDK_NGX_Parameter;
struct NVSDK_NGX_Handle;

class CDlss
{
public:
	// r4_dlss values
	enum
	{
		MODE_OFF = 0,
		MODE_DLAA,
		MODE_QUALITY,
		MODE_BALANCED,
		MODE_PERFORMANCE,
		MODE_ULTRA_PERFORMANCE,
	};

	// r4_dlss_preset values
	enum
	{
		PRESET_DEFAULT = 0,
		PRESET_K,
		PRESET_M,
		PRESET_L,
	};

	// Render size of the r4_dlss mode for this output size; false (logged) when DLSS cannot run
	bool render_size(u32 out_w, u32 out_h, u32& w, u32& h);
	// Upscales color (render size) into output (output size, UAV); false when DLSS failed this frame
	bool evaluate(ID3D11Resource* color, ID3D11Resource* output, ID3D11Resource* depth, ID3D11Resource* mv,
	              u32 render_w, u32 render_h, u32 out_w, u32 out_h, const Fvector2& jitter, bool reset);
	void release_feature();
	void shutdown(); // with the render target and the device

private:
	bool init();
	bool create_feature(u32 render_w, u32 render_h, u32 out_w, u32 out_h);
	u32 preset_hint() const;

	NVSDK_NGX_Parameter* params = nullptr;
	NVSDK_NGX_Handle* feature = nullptr;
	ID3D11Texture2D* exposure = nullptr; // 1x1 R32F = 1.0: the colour is already exposed (LDR)
	bool ngx_initialized = false;
	bool init_tried = false; // NGX init runs (and logs) once per device
	bool create_failed = false; // feature creation failed for this target: bilinear until the next vid_restart
	u32 eval_failures = 0;
	u32 feature_mode = 0;
	u32 feature_w = 0, feature_h = 0, feature_out_w = 0, feature_out_h = 0;
};

extern CDlss Dlss;
