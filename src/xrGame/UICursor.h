#pragma once

#include "ui_base.h"
class CUIStatic;

class CUICursor : public pureRender,
                  public pureScreenResolutionChanged
{
	bool bVisible;
	Fvector2 vPos;
	Fvector2 vPrevPos;
	bool m_b_use_win_cursor;
	// MCP virtual cursor: when owned the Windows cursor is never read or moved (silent runs always own it)
	bool m_mcp_owned;
	bool m_mcp_pending;
	Fvector2 m_mcp_target;
	CUIStatic* m_static;
	void InitInternal();
public:
	CUICursor();
	virtual ~CUICursor();
	virtual void OnRender();

	Fvector2 GetCursorPositionDelta();

	Fvector2 GetCursorPosition();
	void SetUICursorPosition(Fvector2 pos);
	void UpdateCursorPosition(int _dx, int _dy);
	virtual void OnScreenResolutionChanged();

	// MCP: take the cursor and move it to pos on the next UpdateCursorPosition; release returns it to the Windows cursor
	void McpMoveTo(Fvector2 pos);
	void McpRelease();
	bool McpOwned() const { return m_mcp_owned; }

	bool IsVisible() { return bVisible; }
	void Show() { bVisible = true; }
	void Hide() { bVisible = false; }
};
