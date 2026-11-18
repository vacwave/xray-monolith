#include "stdafx.h"
#include "xrParams.h"

#include <magic_enum/magic_enum.hpp>

void xrParams::LoadParams()
{
	xr_string CommandLine = Core.Params;
	auto CommandList = CommandLine.Split(' ');

	for (auto& Command : CommandList)
	{
		if (!Command.StartWith("-"))
			continue;

		Command = Command.substr(1);

		// Special handle for some commands
		if (Command == "60hz") Command = "_" + Command;
		else if (Command == "-dxgi-old") Command = "dxgi_old";
		else if (Command == "-dxgi-dbg") Command = "dxgi_dbg";

		auto EnumData = magic_enum::enum_cast<ECoreParams>(Command);
		if (EnumData.has_value())
		{
			Core.ParamsData.set(EnumData.value(), true);
		}
	}
}

bool xrParams::GetAppDataArg(LPCSTR cmdline, string_path& out)
{
	LPCSTR p = strstr(cmdline, "-appdata ");
	if (!p)
		return false;
	p += xr_strlen("-appdata ");
	while (*p == ' ')
		p++;

	string_path arg = "";
	if (*p == '"')
		sscanf(p + 1, "%[^\"]", arg);
	else
		sscanf(p, "%[^ ]", arg);
	if (!arg[0] || !_fullpath(out, arg, sizeof(out)))
		return false;

	for (char* c = out; *c; c++)
		if (*c == '/')
			*c = '\\';
	size_t len = xr_strlen(out);
	while (len > 3 && out[len - 1] == '\\')
		out[--len] = 0;
	return true;
}
