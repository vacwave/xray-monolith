#include "stdafx.h"
#include "mcp_screenshot.h"
#include "mcp_server.h"
#include "IGame_Level.h"
#include "Render.h"

#include <atomic>
#include <memory>
#include <thread>
#include <chrono>
#include <wincodec.h>
#include <wrl/client.h>
#include <nlohmann/json.hpp>

#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")

using json = nlohmann::json;
using Microsoft::WRL::ComPtr;

namespace mcp_screenshot
{
namespace
{
	enum { st_waiting, st_encoding, st_done };

	// One capture: requested in screenshot.begin, copied by the renderer at the end of the next rendered frame,
	// read back on the main thread in screenshot.get, then encoded on a worker thread (WIC, no COM on the main thread)
	struct job
	{
		u32 id = 0;
		u32 max_width = 1024;
		bool png = false;
		u32 quality = 75;
		std::string save_path; // UTF-8, empty = none
		u32 req_frame = 0, req_rendered = 0; // the capture is the frame rendered right after the request (same on_idle)
		std::string level; // at request time: a level load between request and readback must not relabel the image

		u32 readback_frame = 0;
		float capture_ms = 0;
		CMemoryWriter pixels; // u32 width, u32 height, then BGRX rows

		std::atomic<int> state{ st_waiting };
		bool ok = false;
		std::string result; // JSON text, or the error message
	};

	std::shared_ptr<job> current;
	u32 last_id = 0;

	std::string base64(const u8* d, size_t n)
	{
		static const char t[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
		std::string s;
		s.reserve((n + 2) / 3 * 4);
		for (size_t i = 0; i < n; i += 3)
		{
			u32 v = u32(d[i]) << 16 | (i + 1 < n ? u32(d[i + 1]) << 8 : 0) | (i + 2 < n ? u32(d[i + 2]) : 0);
			s += t[v >> 18 & 63];
			s += t[v >> 12 & 63];
			s += i + 1 < n ? t[v >> 6 & 63] : '=';
			s += i + 2 < n ? t[v & 63] : '=';
		}
		return s;
	}

	HRESULT encode(IWICImagingFactory* wic, IWICBitmapSource* src, IStream* out, bool png, float quality)
	{
		ComPtr<IWICBitmapEncoder> enc;
		ComPtr<IWICBitmapFrameEncode> frame;
		ComPtr<IPropertyBag2> props;
		ComPtr<IWICFormatConverter> conv;
		UINT w = 0, h = 0;
		WICPixelFormatGUID fmt = GUID_WICPixelFormat24bppBGR;

		HRESULT hr = wic->CreateEncoder(png ? GUID_ContainerFormatPng : GUID_ContainerFormatJpeg, nullptr, &enc);
		if (SUCCEEDED(hr)) hr = enc->Initialize(out, WICBitmapEncoderNoCache);
		if (SUCCEEDED(hr)) hr = enc->CreateNewFrame(&frame, &props);
		if (SUCCEEDED(hr) && !png)
		{
			PROPBAG2 opt = {};
			opt.pstrName = const_cast<LPOLESTR>(L"ImageQuality");
			VARIANT v = {};
			v.vt = VT_R4;
			v.fltVal = quality;
			hr = props->Write(1, &opt, &v);
		}
		if (SUCCEEDED(hr)) hr = frame->Initialize(props.Get());
		if (SUCCEEDED(hr)) hr = src->GetSize(&w, &h);
		if (SUCCEEDED(hr)) hr = frame->SetSize(w, h);
		if (SUCCEEDED(hr)) hr = frame->SetPixelFormat(&fmt);
		if (SUCCEEDED(hr)) hr = wic->CreateFormatConverter(&conv);
		if (SUCCEEDED(hr)) hr = conv->Initialize(src, fmt, WICBitmapDitherTypeNone, nullptr, 0.0, WICBitmapPaletteTypeCustom);
		if (SUCCEEDED(hr)) hr = frame->WriteSource(conv.Get(), nullptr);
		if (SUCCEEDED(hr)) hr = frame->Commit();
		if (SUCCEEDED(hr)) hr = enc->Commit();
		return hr;
	}

	std::string hr_str(HRESULT hr)
	{
		char buf[24];
		sprintf_s(buf, "hr=0x%08X", (u32)hr);
		return buf;
	}

	bool ends_with(const std::string& s, const char* suffix)
	{
		size_t n = strlen(suffix);
		return s.size() >= n && _stricmp(s.c_str() + s.size() - n, suffix) == 0;
	}

	// Worker thread; on failure j.result is the error message
	bool encode_job(job& j, IWICImagingFactory* wic)
	{
		auto t0 = std::chrono::steady_clock::now();
		const u8* data = (const u8*)j.pixels.pointer();
		u32 w = ((const u32*)data)[0], h = ((const u32*)data)[1];
		const u8* bits = data + 8;

		ComPtr<IWICBitmap> bmp;
		HRESULT hr = wic->CreateBitmapFromMemory(w, h, GUID_WICPixelFormat32bppBGR, w * 4, w * h * 4, const_cast<u8*>(bits), &bmp);
		if (FAILED(hr))
		{
			j.result = "CreateBitmapFromMemory failed: " + hr_str(hr);
			return false;
		}

		u32 out_w = w, out_h = h;
		if (j.max_width && w > j.max_width)
		{
			out_w = j.max_width;
			out_h = std::max<u32>(1, u32((u64)h * out_w / w));
		}
		ComPtr<IWICBitmapSource> src = bmp;
		if (out_w != w)
		{
			ComPtr<IWICBitmapScaler> scaler;
			hr = wic->CreateBitmapScaler(&scaler);
			if (SUCCEEDED(hr)) hr = scaler->Initialize(bmp.Get(), out_w, out_h, WICBitmapInterpolationModeFant);
			if (FAILED(hr))
			{
				j.result = "downscale failed: " + hr_str(hr);
				return false;
			}
			src = scaler;
		}

		ComPtr<IStream> mem;
		hr = CreateStreamOnHGlobal(nullptr, TRUE, &mem);
		if (SUCCEEDED(hr)) hr = encode(wic, src.Get(), mem.Get(), j.png, j.quality / 100.f);
		ULARGE_INTEGER size = {};
		if (SUCCEEDED(hr)) hr = mem->Seek(LARGE_INTEGER{}, STREAM_SEEK_CUR, &size);
		HGLOBAL g = nullptr;
		if (SUCCEEDED(hr)) hr = GetHGlobalFromStream(mem.Get(), &g);
		if (FAILED(hr))
		{
			j.result = "encode failed: " + hr_str(hr);
			return false;
		}
		std::string b64;
		if (void* p = GlobalLock(g))
		{
			b64 = base64((const u8*)p, (size_t)size.QuadPart);
			GlobalUnlock(g);
		}
		auto t1 = std::chrono::steady_clock::now();

		json saved = nullptr;
		json saved_bytes = nullptr;
		if (!j.save_path.empty())
		{
			int n = MultiByteToWideChar(CP_UTF8, 0, j.save_path.c_str(), -1, nullptr, 0);
			std::wstring wpath(n > 0 ? n : 1, L'\0');
			if (n > 0)
				MultiByteToWideChar(CP_UTF8, 0, j.save_path.c_str(), -1, &wpath[0], n);
			bool file_png = !(ends_with(j.save_path, ".jpg") || ends_with(j.save_path, ".jpeg"));
			ComPtr<IWICStream> file;
			ULARGE_INTEGER fsize = {};
			hr = wic->CreateStream(&file);
			if (SUCCEEDED(hr)) hr = file->InitializeFromFilename(wpath.c_str(), GENERIC_WRITE);
			if (SUCCEEDED(hr)) hr = encode(wic, bmp.Get(), file.Get(), file_png, 0.95f);
			if (SUCCEEDED(hr)) hr = file->Seek(LARGE_INTEGER{}, STREAM_SEEK_CUR, &fsize);
			if (FAILED(hr))
			{
				j.result = "saving " + j.save_path + " failed: " + hr_str(hr);
				return false;
			}
			saved = j.save_path;
			saved_bytes = fsize.QuadPart;
		}
		auto t2 = std::chrono::steady_clock::now();

		json r = {
			{ "id", j.id },
			{ "frame", j.req_frame },
			{ "readback_frame", j.readback_frame },
			{ "level", j.level.empty() ? json(nullptr) : json(j.level) },
			{ "width", out_w },
			{ "height", out_h },
			{ "orig_width", w },
			{ "orig_height", h },
			{ "format", j.png ? "png" : "jpeg" },
			{ "bytes", size.QuadPart },
			{ "saved_path", saved },
			{ "saved_bytes", saved_bytes },
			{ "capture_ms", j.capture_ms },
			{ "encode_ms", std::chrono::duration<float, std::milli>(t1 - t0).count() },
			{ "save_ms", std::chrono::duration<float, std::milli>(t2 - t1).count() },
			{ "data", std::move(b64) },
		};
		j.result = r.dump(-1, ' ', false, json::error_handler_t::replace);
		return true;
	}

	void start_encode(const std::shared_ptr<job>& jp)
	{
		jp->state = st_encoding;
		std::thread([jp]()
		{
			HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
			{
				ComPtr<IWICImagingFactory> wic;
				HRESULT hr = CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&wic));
				if (FAILED(hr))
					jp->result = "WIC is unavailable: " + hr_str(hr);
				else
					jp->ok = encode_job(*jp, wic.Get());
			}
			if (SUCCEEDED(co))
				CoUninitialize();
			jp->state = st_done;
		}).detach();
	}

	bool get_param(const json& p, const char* key, json& out)
	{
		auto it = p.find(key);
		if (it == p.end() || it->is_null())
			return false;
		out = *it;
		return true;
	}
}

void register_methods()
{
	// {"max_width":1024 (0 = full size),"format":"jpeg"|"png","quality":1..100,"save_path":"<abs path>"} -> {"id","frame"}
	mcp::register_method("screenshot.begin", [](LPCSTR params, xr_string& out)
	{
		json p = json::parse(params, nullptr, false);
		if (!p.is_object())
			p = json::object();
		auto j = std::make_shared<job>();
		json v;
		if (get_param(p, "max_width", v))
		{
			if (!v.is_number_unsigned())
			{
				out = "max_width must be an integer >= 0";
				return false;
			}
			j->max_width = v.get<u32>();
		}
		if (get_param(p, "format", v))
		{
			std::string f = v.is_string() ? v.get<std::string>() : "";
			if (f == "png")
				j->png = true;
			else if (f != "jpeg" && f != "jpg")
			{
				out = "format must be \"jpeg\" or \"png\"";
				return false;
			}
		}
		if (get_param(p, "quality", v))
		{
			if (!v.is_number_integer() || v.get<s64>() < 1 || v.get<s64>() > 100)
			{
				out = "quality must be 1..100";
				return false;
			}
			j->quality = v.get<u32>();
		}
		if (get_param(p, "save_path", v))
		{
			if (!v.is_string())
			{
				out = "save_path must be a string";
				return false;
			}
			j->save_path = v.get<std::string>();
		}
		if (!Render || !Device.b_is_Ready)
		{
			out = "renderer is not ready";
			return false;
		}

		// The copy happens in the renderer's End() of the next rendered frame (back buffer incl. UI, before Present)
		Render->ScreenshotAsyncBegin();
		j->id = ++last_id;
		j->req_frame = Device.dwFrame;
		j->req_rendered = Device.dwRenderedFrames;
		shared_str level = g_pGameLevel ? g_pGameLevel->name() : shared_str(); // empty while loading
		if (level.size())
			j->level = level.c_str();
		current = j; // a capture still waiting is superseded; one being encoded finishes on its own
		out = json({ { "id", j->id }, { "frame", j->req_frame } }).dump().c_str();
		return true;
	});

	// {"id":N} -> {"pending":true} until encoded, then the screenshot.begin result (see encode_job)
	mcp::register_method("screenshot.get", [](LPCSTR params, xr_string& out)
	{
		s64 id = 0;
		if (!mcp::get_int_param(params, "id", id) || !current || current->id != id)
		{
			out = "unknown screenshot id (superseded by a newer screenshot.begin, or already fetched)";
			return false;
		}
		job& j = *current;
		if (j.state == st_waiting)
		{
			if (Device.dwRenderedFrames == j.req_rendered)
			{
				out = "{\"pending\":true,\"stage\":\"frame\"}";
				return true;
			}
			auto t0 = std::chrono::steady_clock::now();
			Render->ScreenshotAsyncEnd(j.pixels);
			j.capture_ms = std::chrono::duration<float, std::milli>(std::chrono::steady_clock::now() - t0).count();
			j.readback_frame = Device.dwFrame;
			const u32* hdr = (const u32*)j.pixels.pointer();
			if (j.pixels.size() < 8 || !hdr[0] || !hdr[1] || j.pixels.size() != 8 + size_t(hdr[0]) * hdr[1] * 4)
			{
				current.reset();
				out = "capture failed (no readable back buffer copy; HDR10 output is not supported)";
				return false;
			}
			start_encode(current);
		}
		if (j.state != st_done)
		{
			out = "{\"pending\":true,\"stage\":\"encode\"}";
			return true;
		}
		bool ok = j.ok;
		out = j.result.c_str();
		current.reset();
		return ok;
	});
}
}
