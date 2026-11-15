# Input
- `CInput` xrEngine/xr_input.h:16 = DirectInput8 buffered; keyboard `KeyUpdate` Xr_input.cpp:190 → `cbStack.back()->IR_OnKeyboardPress/Release/Hold` :255-268; mouse `MouseUpdate` :434 → IR_OnMouseMove/Wheel :626.
- `CLevel::IR_OnKeyboardPress` Level_input.cpp:160 maps key → action via `get_binded_action`; action ids (kFWD…) in xrGame/xr_level_controller.cpp.
- Existing simulation: Lua `level.press_action/release_action/hold_action` level_script.cpp:1294-1310 (bound :3174) call `Level().IR_OnKeyboardPress(cmd)`. Mouse look needs a C++ `IR_OnMouseMove` call.
