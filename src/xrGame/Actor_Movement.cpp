#include "stdafx.h"

#include "actor.h"
#include "inventory.h"
#include "weapon.h"
#include "../xrEngine/CameraBase.h"
#include "xrMessages.h"

#include "level.h"
#include "UIGameCustom.h"
#include "string_table.h"
#include "actorcondition.h"
#include "game_cl_base.h"
#include "WeaponMagazined.h"
#include "CharacterPhysicsSupport.h"
#include "actoreffector.h"
#include "static_cast_checked.hpp"
#include "player_hud.h"

#ifdef DEBUG
#include "phdebug.h"
#endif
BOOL disableActorBodyRotationDelay = TRUE;

// verdatim: flag to allow 'running' speed during left or right lookouts
BOOL AllowAccelDuringLookOut = FALSE; 
static const float s_fLandingTime1 = 0.1f; // через сколько снять флаг Landing1 (т.е. включить следующую анимацию)
static const float s_fLandingTime2 = 0.3f; // через сколько снять флаг Landing2 (т.е. включить следующую анимацию)
static const float s_fJumpTime = 0.3f;
static const float s_fJumpGroundTime = 0.1f; // для снятия флажка Jump если на земле
const float s_fFallTime = 0.2f;

BOOL g_actor_overweight_rework = TRUE;
BOOL g_actor_parkour = TRUE;

//////////////////////////////////////////////////////////////////////////
// Overweight rework tuning
// r = TotalWeight / MaxCarryWeight, r_max = MaxWalkWeight / MaxCarryWeight (can't-walk weight)
// All curves are piecewise-linear between anchors, clamped at the ends.
//////////////////////////////////////////////////////////////////////////
namespace overweight_tune
{
	static const float r_overweight		= 0.33f;	// speed + turn penalties start
	static const float r_heavy			= 0.60f;	// extra stamina drain starts
	static const float r_full			= 1.00f;	// full carry weight

	// movement speed multiplier (1 at r_overweight)
	static const float speed_at_heavy	= 0.96f;
	static const float speed_at_full	= 0.90f;
	static const float speed_at_max		= 0.60f;

	// camera turn speed multiplier (1 at r_overweight)
	static const float look_at_heavy	= 0.9625f;
	static const float look_at_full		= 0.8875f;
	static const float look_at_max		= 0.55f;

	// walk stamina drain multiplier (1 at r_heavy)
	static const float stamina_at_full	= 1.125f;
	static const float stamina_at_max	= 2.00f;

	// inertia strength 0..1
	static const float r_inertia_low	= 0.70f;	// end of the light-load inertia band
	static const float inertia_base		= 0.05f;	// r = 0
	static const float inertia_at_low	= 0.15f;	// r = r_inertia_low
	static const float inertia_at_full	= 0.25f;	// r = r_full
	static const float inertia_at_max	= 1.00f;	// r = r_max
	// shape of the r_full -> r_max segment: 1 = linear, >1 = stays low longer, then rises steeply near r_max
	static const float inertia_high_curve = 2.5f;

	// start-up curve: inertia effect while speeding up scales from start_floor (standing still)
	// to 1 (at target speed) along (speed fraction ^ start_curve); >1 = snappier start
	static const float inertia_start_floor = 0.20f;
	static const float inertia_start_curve = 2.0f;

	// low-speed scaling: inertia effect scales from lowspeed_floor (barely moving) to 1 at walk speed
	// along (speed fraction ^ lowspeed_curve); speed fraction = accel magnitude / m_fWalkAccel
	static const float inertia_lowspeed_floor = 0.10f;
	static const float inertia_lowspeed_curve = 1.5f;
	// above r_full the curve exponent rises linearly to this value at r_max (slow movement stays responsive when heavy)
	static const float inertia_lowspeed_curve_max = 3.5f;

	// smoothing time constant (seconds) at inertia 1.0; scaled linearly by inertia strength
	static const float accel_time_full	= 2.0f;		// speeding up / changing direction
	static const float decel_time_full	= 1.2f;		// slowing down / stopping
	// with no input, snap smoothed accel to zero below this magnitude
	// (CPHActorCharacter::SetAcceleration ignores changes smaller than 0.5)
	static const float stop_snap_accel	= 1.0f;
}

static float overweight_lerp(float r, float r0, float v0, float r1, float v1)
{
	if (r <= r0)
		return v0;
	if (r >= r1)
		return v1;
	if (r1 - r0 <= EPS)
		return v1;
	return v0 + (v1 - v0) * (r - r0) / (r1 - r0);
}

// r = TotalWeight / MaxCarryWeight (0 when disabled, godmode or not single player)
// r_max = MaxWalkWeight / MaxCarryWeight (can't-walk ratio), kept above r_full
static void overweight_ratios(const CActor* actor, float& r, float& r_max)
{
	using namespace overweight_tune;
	r = 0.f;
	r_max = r_full + 0.01f;
	if (!g_actor_overweight_rework || !IsGameTypeSingle() || GodMode())
		return;
	float max_w = actor->MaxCarryWeight();
	if (max_w <= EPS)
		return;
	r = actor->inventory().TotalWeight() / max_w;
	r_max = _max(actor->MaxWalkWeight() / max_w, r_full + 0.01f);
}

// piecewise-linear through (r_overweight, v_ov) (r_heavy, v_heavy) (r_full, v_full) (r_max, v_max)
static float overweight_curve(float r, float r_max, float v_ov, float v_heavy, float v_full, float v_max)
{
	using namespace overweight_tune;
	if (r < r_heavy)
		return overweight_lerp(r, r_overweight, v_ov, r_heavy, v_heavy);
	if (r < r_full)
		return overweight_lerp(r, r_heavy, v_heavy, r_full, v_full);
	return overweight_lerp(r, r_full, v_full, r_max, v_max);
}

// movement inertia strength 0..1
static float overweight_inertia(float r, float r_max)
{
	using namespace overweight_tune;
	float res;
	if (r < r_inertia_low)
		res = overweight_lerp(r, 0.f, inertia_base, r_inertia_low, inertia_at_low);
	else if (r < r_full)
		res = overweight_lerp(r, r_inertia_low, inertia_at_low, r_full, inertia_at_full);
	else
	{
		float u = overweight_lerp(r, r_full, 0.f, r_max, 1.f);
		res = inertia_at_full + (inertia_at_max - inertia_at_full) * powf(u, inertia_high_curve);
	}
	clamp(res, 0.f, 1.f);
	return res;
}

IC static void generate_orthonormal_basis1(const Fvector& dir, Fvector& updir, Fvector& right)
{
	right.crossproduct(dir, updir); //. <->
	right.normalize();
	updir.crossproduct(right, dir);
}


void CActor::g_cl_ValidateMState(float dt, u32 mstate_wf)
{
	if (m_parkour_slide)
		mstate_wf |= mcCrouch; // stay crouched until the slide ends
	// Lookout
	if (((mstate_wf & mcLLookout) && (mstate_wf & mcRLookout)) || ((mstate_real & mcLLookout) && (mstate_real & mcRLookout)))
	{
		// It's impossible to perform right and left lookouts in the same time
		mstate_real &= ~mcLookout;
	}
	else if (mstate_wf & mcLookout)
	{
		// Activate one of lookouts
		mstate_real |= mstate_wf & mcLookout;
	}
	else
	{
		// No lookouts needed
		mstate_real &= ~mcLookout;
	}

	if (mstate_real & (mcJump | mcFall | mcLanding | mcLanding2))
		mstate_real &= ~mcLookout;

	// закончить приземление
	if (mstate_real & (mcLanding | mcLanding2))
	{
		m_fLandingTime -= dt;
		if (m_fLandingTime <= 0.f)
		{
			mstate_real &= ~ (mcLanding | mcLanding2);
			mstate_real &= ~ (mcFall | mcJump);
		}
	}
	// закончить падение
	if (character_physics_support()->movement()->gcontact_Was)
	{
		if (mstate_real & mcFall)
		{
			if (character_physics_support()->movement()->GetContactSpeed() > 4.f)
			{
				if (fis_zero(character_physics_support()->movement()->gcontact_HealthLost))
				{
					m_fLandingTime = s_fLandingTime1;
					mstate_real |= mcLanding;
				}
				else
				{
					m_fLandingTime = s_fLandingTime2;
					mstate_real |= mcLanding2;
				}
			}

			::luabind::functor<bool> on_land;
			if (ai().script_engine().functor("_G.CActor_on_land", on_land))
				on_land(character_physics_support()->movement()->GetContactSpeed());
		}
		m_bJumpKeyPressed = TRUE;
		m_fJumpTime = s_fJumpTime;
		mstate_real &= ~ (mcFall | mcJump);
	}
	if ((mstate_wf & mcJump) == 0)
		m_bJumpKeyPressed = FALSE;

	// Зажало-ли меня/уперся - не двигаюсь
	if (((character_physics_support()->movement()->GetVelocityActual() < 0.2f) && (!(mstate_real & (mcFall | mcJump | mcLanding | mcLanding2)) || (!(mstate_real & mcClimb) && character_physics_support()->movement()->Environment() == CPHMovementControl::peAtWall)))
		|| character_physics_support()->movement()->bSleep)
	{
		mstate_real &= ~ mcAnyMove;
	}
	if (character_physics_support()->movement()->Environment() == CPHMovementControl::peOnGround ||
		character_physics_support()->movement()->Environment() == CPHMovementControl::peAtWall)
	{
		// если на земле гарантированно снимать флажок Jump
		if (((s_fJumpTime - m_fJumpTime) > s_fJumpGroundTime) && (mstate_real & mcJump))
		{
			mstate_real &= ~ mcJump;
			m_fJumpTime = s_fJumpTime;
		}
	}
	if (character_physics_support()->movement()->Environment() == CPHMovementControl::peAtWall)
	{
		if (!(mstate_real & mcClimb))
		{
			mstate_real |= mcClimb;
			mstate_real &= ~mcSprint;
			cam_SetLadder();
		}
	}
	else
	{
		if (mstate_real & mcClimb)
		{
			cam_UnsetLadder();
		}
		mstate_real &= ~mcClimb;
	};

	if (mstate_wf != mstate_real)
	{
		if ((mstate_real & mcCrouch) && ((0 == (mstate_wf & mcCrouch)) || mstate_real & mcClimb))
		{
			if (character_physics_support()->movement()->ActivateBoxDynamic(0))
			{
				mstate_real &= ~mcCrouch;
			}
		}
	}

	if (!CanAccelerate() && isActorAccelerated(mstate_real, IsZoomAimingMode()))
	{
		mstate_real ^= mcAccel;
	};

	if (this == Level().CurrentControlEntity())
	{
		bool bOnClimbNow = !!(mstate_real & mcClimb);
		bool bOnClimbOld = !!(mstate_old & mcClimb);

		if (bOnClimbNow != bOnClimbOld)
		{
			SetWeaponHideState(/*INV_STATE_LADDER*/ INV_STATE_BLOCK_ALL, bOnClimbNow);
		};
	};
};

void CActor::g_cl_CheckControls(u32 mstate_wf, Fvector& vControlAccel, float& Jump, float dt)
{
	float cam_eff_factor = 0.0f;
	mstate_old = mstate_real;
	vControlAccel.set(0, 0, 0);

	if (!(mstate_real & mcFall) && (character_physics_support()->movement()->Environment() == CPHMovementControl::
		peInAir))
	{
		m_fFallTime -= dt;
		if (m_fFallTime <= 0.f)
		{
			m_fFallTime = s_fFallTime;
			mstate_real |= mcFall;
			mstate_real &= ~ mcJump;
		}
	}

	if (!CanMove())
	{
		if (mstate_wf & mcAnyMove)
		{
			StopAnyMove();
			mstate_wf &= ~mcAnyMove;
			mstate_wf &= ~mcJump;
		}
	}
	if (parkour_Mantle(mstate_wf))
		return;
	// update player accel
	if (mstate_wf & mcFwd) vControlAccel.z += 1;
	if (mstate_wf & mcBack) vControlAccel.z += -1;
	if (mstate_wf & mcLStrafe) mstate_wf & mcSprint ? vControlAccel.x += -0.5 : vControlAccel.x += -1;
	if (mstate_wf & mcRStrafe) mstate_wf & mcSprint ? vControlAccel.x += 0.5 : vControlAccel.x += 1;

	CPHMovementControl::EEnvironment curr_env = character_physics_support()->movement()->Environment();
	if (curr_env == CPHMovementControl::peOnGround || curr_env == CPHMovementControl::peAtWall)
	{
		// crouch
		if ((0 == (mstate_real & mcCrouch)) && (mstate_wf & mcCrouch))
		{
			if (mstate_real & mcClimb)
			{
				mstate_wf &= ~mcCrouch;
			}
			else
			{
				character_physics_support()->movement()->EnableCharacter();
				bool Crouched = false;
				if (isActorAccelerated(mstate_wf, IsZoomAimingMode()))
					Crouched = character_physics_support()->movement()->ActivateBoxDynamic(1);
				else
					Crouched = character_physics_support()->movement()->ActivateBoxDynamic(2);

				if (Crouched)
					mstate_real |= mcCrouch;
			}
		}
		// jump
		m_fJumpTime -= dt;

		if (CanJump() && (mstate_wf & mcJump))
		{
			mstate_real |= mcJump;
			m_bJumpKeyPressed = TRUE;
			Jump = m_fJumpSpeed;
			m_fJumpTime = s_fJumpTime;

			::luabind::functor<bool> on_jump;
			if (ai().script_engine().functor("_G.CActor_on_jump", on_jump))
				on_jump();

			//уменьшить силу игрока из-за выполненого прыжка
			if (!GodMode())
				conditions().ConditionJump(inventory().TotalWeight() / MaxCarryWeight());
		}

		// mask input into "real" state
		u32 move = mcAnyMove | mcAccel;

		if (mstate_real & mcCrouch)
		{
			if (!isActorAccelerated(mstate_real, IsZoomAimingMode()) && isActorAccelerated(
				mstate_wf, IsZoomAimingMode()))
			{
				character_physics_support()->movement()->EnableCharacter();
				if (!character_physics_support()->movement()->ActivateBoxDynamic(1))move &= ~mcAccel;
			}

			if (isActorAccelerated(mstate_real, IsZoomAimingMode()) && !isActorAccelerated(
				mstate_wf, IsZoomAimingMode()))
			{
				character_physics_support()->movement()->EnableCharacter();
				if (character_physics_support()->movement()->ActivateBoxDynamic(2))mstate_real &= ~mcAccel;
			}
		}

		if ((mstate_wf & mcSprint) && !CanSprint())
			mstate_wf &= ~mcSprint;

		mstate_real &= (~move);
		mstate_real |= (mstate_wf & move);

		if (mstate_wf & mcSprint)
			mstate_real |= mcSprint;
		else
			mstate_real &= ~mcSprint;
		if (!(mstate_real & (mcFwd)) || mstate_real & (mcCrouch | mcClimb) || !isActorAccelerated(
			mstate_wf, IsZoomAimingMode()))
		{
			mstate_real &= ~mcSprint;
			if (!(mstate_real & (mcCrouch) && !(mstate_wf & mcCrouch)))
				mstate_wishful &= ~mcSprint;
		}

		// check player move state
		if (mstate_real & mcAnyMove)
		{
			BOOL bAccelerated = isActorAccelerated(mstate_real, IsZoomAimingMode()) && CanAccelerate();

			// correct "mstate_real" if opposite keys pressed
			if (_abs(vControlAccel.z) < EPS) mstate_real &= ~(mcFwd + mcBack);
			if (_abs(vControlAccel.x) < EPS) mstate_real &= ~(mcLStrafe + mcRStrafe);

			// normalize and analyze crouch and run
			float scale = vControlAccel.magnitude();
			if (scale > EPS)
			{
				scale = m_fWalkAccel / scale;
				if (bAccelerated && !IsZoomAimingMode())
					if (mstate_real & mcBack)
						scale *= m_fRunBackFactor;
					else
						scale *= m_fRunFactor;
				else if (mstate_real & mcBack)
					scale *= m_fWalkBackFactor;


				if (mstate_real & mcCrouch) scale *= m_fCrouchFactor;
				if (mstate_real & mcClimb) scale *= m_fClimbFactor;
				if (mstate_real & mcSprint) scale *= m_fSprintFactor;

				if (mstate_real & (mcLStrafe | mcRStrafe) && !(mstate_real & mcCrouch))
				{
					if (mstate_real & mcSprint)
						scale *= m_fSprint_StrafeFactor;
					else if (bAccelerated)
						scale *= m_fRun_StrafeFactor;
					else
						scale *= m_fWalk_StrafeFactor;
				}

				scale *= OverweightSpeedFactor(); // overweight speed penalty

				vControlAccel.mul(scale);
				cam_eff_factor = scale;
			} //scale>EPS
		} //(mstate_real&mcAnyMove)
	} //peOnGround || peAtWall

	if (IsGameTypeSingle() && cam_eff_factor > EPS)
	{
		LPCSTR state_anm = NULL;

		if (mstate_real & mcSprint && !(mstate_old & mcSprint))
			state_anm = "sprint";
		else if (mstate_real & mcLStrafe && !(mstate_old & mcLStrafe))
			state_anm = "strafe_left";
		else if (mstate_real & mcRStrafe && !(mstate_old & mcRStrafe))
			state_anm = "strafe_right";
		else if (mstate_real & mcFwd && !(mstate_old & mcFwd))
			state_anm = "move_fwd";
		else if (mstate_real & mcBack && !(mstate_old & mcBack))
			state_anm = "move_back";

		if (state_anm)
		{
			//play moving cam effect
			CActor* control_entity = static_cast_checked<CActor*>(Level().CurrentControlEntity());
			if (control_entity)
			{
				R_ASSERT2(control_entity, "current control entity is NULL");
				CEffectorCam* ec = control_entity->Cameras().GetCamEffector(eCEActorMoving);
				if (NULL == ec)
				{
					string_path			eff_name;
					xr_sprintf(eff_name, sizeof(eff_name), "%s.anm", state_anm);
					string_path			ce_path;
					string_path			anm_name;
					strconcat(sizeof(anm_name), anm_name, "camera_effects\\actor_move\\", eff_name);
					if (FS.exist(ce_path, "$game_anims$", anm_name))
					{
						CAnimatorCamLerpEffectorConst* e = xr_new<CAnimatorCamLerpEffectorConst>();
						float max_scale = 70.0f;
						float factor = cam_eff_factor / max_scale;
						e->SetFactor(factor);
						e->SetType(eCEActorMoving);
						e->SetHudAffect(false);
						e->SetCyclic(false);
						e->Start(anm_name);
						control_entity->Cameras().AddCamEffector(e);
					}
				}
			}
		}
	}
	//transform local dir to world dir
	Fmatrix mOrient;
	mOrient.rotateY(-r_model_yaw);
	mOrient.transform_dir(vControlAccel);

	if (parkour_Slide(vControlAccel, dt))
		return;
	// inertia is applied in world space so turning the camera does not instantly redirect momentum
	ApplyMovementInertia(vControlAccel, dt);
}

void CActor::ApplyMovementInertia(Fvector& vControlAccel, float dt)
{
	using namespace overweight_tune;

	if (!g_actor_overweight_rework || !IsGameTypeSingle() || (mstate_real & mcClimb) || dt <= 0.f)
	{
		m_vInertiaAccel.set(vControlAccel);
		return;
	}

	CPHMovementControl::EEnvironment env = character_physics_support()->movement()->Environment();
	if (env != CPHMovementControl::peOnGround && env != CPHMovementControl::peAtWall)
		return; // airborne: raw air control passes through, momentum state frozen

	float r, r_max;
	overweight_ratios(this, r, r_max);
	float inertia = overweight_inertia(r, r_max);
	float target_mag = vControlAccel.magnitude();
	float cur_mag = m_vInertiaAccel.magnitude();
	float speed_f = m_fWalkAccel > EPS ? _max(target_mag, cur_mag) / m_fWalkAccel : 1.f;
	clamp(speed_f, 0.f, 1.f);
	float low_curve = overweight_lerp(r, r_full, inertia_lowspeed_curve, r_max, inertia_lowspeed_curve_max);
	inertia *= inertia_lowspeed_floor + (1.f - inertia_lowspeed_floor) * powf(speed_f, low_curve);

	float t;
	if (target_mag >= cur_mag)
	{
		float f = target_mag > EPS ? cur_mag / target_mag : 1.f;
		clamp(f, 0.f, 1.f);
		float start_k = inertia_start_floor + (1.f - inertia_start_floor) * powf(f, inertia_start_curve);
		t = inertia * accel_time_full * start_k;
	}
	else
		t = inertia * decel_time_full;
	if (t > EPS)
		m_vInertiaAccel.lerp(m_vInertiaAccel, vControlAccel, 1.f - expf(-dt / t));
	else
		m_vInertiaAccel.set(vControlAccel);

	if (target_mag < EPS && m_vInertiaAccel.magnitude() < stop_snap_accel)
		m_vInertiaAccel.set(0.f, 0.f, 0.f);

	vControlAccel.set(m_vInertiaAccel);
}

#define ACTOR_ANIM_SECT "actor_animation"

#define ACTOR_LLOOKOUT_ANGLE	PI_DIV_4
#define ACTOR_RLOOKOUT_ANGLE	PI_DIV_4

void CActor::g_Orientate(u32 mstate_rl, float dt)
{
	static float fwd_l_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "fwd_l_strafe_yaw"));
	static float back_l_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "back_l_strafe_yaw"));
	static float fwd_r_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "fwd_r_strafe_yaw"));
	static float back_r_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "back_r_strafe_yaw"));
	static float l_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "l_strafe_yaw"));
	static float r_strafe_yaw = deg2rad(pSettings->r_float(ACTOR_ANIM_SECT, "r_strafe_yaw"));

	if (!g_Alive())return;
	// visual effect of "fwd+strafe" like motion
	float calc_yaw = 0;
	if (mstate_real & mcClimb)
	{
		if (g_LadderOrient()) return;
	}
	switch (mstate_rl & mcAnyMove)
	{
	case mcFwd + mcLStrafe:
		calc_yaw = +fwd_l_strafe_yaw; //+PI_DIV_4; 
		break;
	case mcBack + mcRStrafe:
		calc_yaw = +back_r_strafe_yaw; //+PI_DIV_4; 
		break;
	case mcFwd + mcRStrafe:
		calc_yaw = -fwd_r_strafe_yaw; //-PI_DIV_4; 
		break;
	case mcBack + mcLStrafe:
		calc_yaw = -back_l_strafe_yaw; //-PI_DIV_4; 
		break;
	case mcLStrafe:
		calc_yaw = +l_strafe_yaw; //+PI_DIV_3-EPS_L; 
		break;
	case mcRStrafe:
		calc_yaw = -r_strafe_yaw; //-PI_DIV_4+EPS_L; 
		break;
	}

	// lerp angle for "effect" and capture torso data from camera
	float scale_yaw_offset = 1.0f;

	if (cam_active == eacFirstEye && g_player_hud && m_legs_controller.is_active()) {
		scale_yaw_offset = 0.15f;
	}

	angle_lerp(r_model_yaw_delta, calc_yaw * scale_yaw_offset, PI_MUL_4 * scale_yaw_offset, dt);

	// build matrix
	Fmatrix mXFORM;
	mXFORM.rotateY(-(r_model_yaw + r_model_yaw_delta));
	mXFORM.c.set(Position());
	XFORM().set(mXFORM);
	VERIFY(_valid(XFORM()));

	//-------------------------------------------------

	float tgt_roll = 0.f;
	if (mstate_rl & mcLookout)
	{
		tgt_roll = (mstate_rl & mcLLookout) ? -ACTOR_LLOOKOUT_ANGLE : ACTOR_RLOOKOUT_ANGLE;

		// demonized: add lookout modifier
		tgt_roll *= m_fLookoutFactor;

		if ((mstate_rl & mcLLookout) && (mstate_rl & mcRLookout))
			tgt_roll = 0.0f;
	}
	if (!fsimilar(tgt_roll, r_torso_tgt_roll, EPS))
	{
		r_torso_tgt_roll = angle_inertion_var(r_torso_tgt_roll, tgt_roll, 0.f, CurrentHeight * PI_MUL_2, PI_DIV_2, dt);
		r_torso_tgt_roll = angle_normalize_signed(r_torso_tgt_roll);
	}
}

bool CActor::g_LadderOrient()
{
	Fvector leader_norm;
	character_physics_support()->movement()->GroundNormal(leader_norm);
	if (_abs(leader_norm.y) > M_SQRT1_2) return false;
	//leader_norm.y=0.f;
	float mag = leader_norm.magnitude();
	if (mag < EPS_L) return false;
	leader_norm.div(mag);
	leader_norm.invert();
	Fmatrix M;
	M.set(Fidentity);
	M.k.set(leader_norm);
	M.j.set(0.f, 1.f, 0.f);
	generate_orthonormal_basis1(M.k, M.j, M.i);
	M.i.invert();
	//M.j.invert();


	//Fquaternion q1,q2,q3;
	//q1.set(XFORM());
	//q2.set(M);
	//q3.slerp(q1,q2,dt);
	//Fvector angles1,angles2,angles3;
	//XFORM().getHPB(angles1.x,angles1.y,angles1.z);
	//M.getHPB(angles2.x,angles2.y,angles2.z);
	////angle_lerp(angles3.x,angles1.x,angles2.x,dt);
	////angle_lerp(angles3.y,angles1.y,angles2.y,dt);
	////angle_lerp(angles3.z,angles1.z,angles2.z,dt);

	//angles3.lerp(angles1,angles2,dt);
	////angle_lerp(angles3.y,angles1.y,angles2.y,dt);
	////angle_lerp(angles3.z,angles1.z,angles2.z,dt);
	//angle_lerp(angles3.x,angles1.x,angles2.x,dt);
	//XFORM().setHPB(angles3.x,angles3.y,angles3.z);
	Fvector position;
	position.set(Position());
	//XFORM().rotation(q3);
	VERIFY2(_valid(M), "Invalide matrix in g_LadderOrient");
	XFORM().set(M);
	VERIFY2(_valid(position), "Invalide position in g_LadderOrient");
	Position().set(position);
	VERIFY(_valid(XFORM()));
	return true;
}

// ****************************** Update actor orientation according to camera orientation
void CActor::g_cl_Orientate(u32 mstate_rl, float dt)
{
	// capture camera into torso (only for FirstEye & LookAt cameras)
	if (eacFreeLook != cam_active)
	{
		if (cam_freelook == eflDisabled)
		{
			r_torso.yaw = cam_Active()->GetWorldYaw();
			r_torso.pitch = cam_Active()->GetWorldPitch();
		}
		else
		{
			CCameraBase* C = cam_Active();
			r_torso.yaw = angle_lerp(cam_Active()->GetWorldYaw(), -old_torso_yaw, freelook_cam_control);
			float old_pitch = cam_Active()->GetWorldPitch();
			float new_pitch = old_pitch > 0.f ? old_pitch * .6f : old_pitch *.8f;
			r_torso.pitch = angle_lerp(old_pitch, new_pitch, freelook_cam_control);
		}
	}
	else
	{
		r_torso.yaw = cam_FirstEye()->GetWorldYaw();
		r_torso.pitch = cam_FirstEye()->GetWorldPitch();
	}

	unaffected_r_torso.yaw = r_torso.yaw;
	unaffected_r_torso.pitch = r_torso.pitch;
	unaffected_r_torso.roll = r_torso.roll;

	CWeaponMagazined* pWM = smart_cast<CWeaponMagazined*>(inventory().GetActiveSlot() != NO_ACTIVE_SLOT
		                                                      ? inventory().ItemFromSlot(inventory().GetActiveSlot())
		                                                      : NULL);
	if (pWM && pWM->GetCurrentFireMode() == 1 && eacFirstEye != cam_active)
	{
		Fvector dangle = weapon_recoil_last_delta();
		r_torso.yaw = unaffected_r_torso.yaw + dangle.y;
		r_torso.pitch = unaffected_r_torso.pitch + dangle.x;
	}

    if (disableActorBodyRotationDelay || Device.time_factor() < 1)
    {
        r_model_yaw = angle_normalize(r_torso.yaw);
        r_model_yaw_dest = r_model_yaw;
        mstate_real &= ~mcTurn;
    }
    else
    {
        // если есть движение - выровнять модель по камере
        if (mstate_rl & mcAnyMove)
        {
            r_model_yaw = angle_normalize(r_torso.yaw);
            mstate_real &= ~mcTurn;
        }
        else
        {
            // if camera rotated more than 45 degrees - align model with it
            float ty = angle_normalize(r_torso.yaw);
            if (_abs(r_model_yaw - ty) > PI_DIV_4 - 30)
            {
                r_model_yaw_dest = ty;
                // 
                mstate_real |= mcTurn;
            }
            if (_abs(r_model_yaw - r_model_yaw_dest) < EPS_L)
            {
                mstate_real &= ~mcTurn;
            }
            if (mstate_rl & mcTurn)
            {
                angle_lerp(r_model_yaw, r_model_yaw_dest, PI_MUL_2, dt);
            }
        }
    }
}

void CActor::g_sv_Orientate(u32 /**mstate_rl/**/, float /**dt/**/)
{
	r_model_yaw = NET_Last.o_model;

	r_torso.yaw = unaffected_r_torso.yaw;
	r_torso.pitch = unaffected_r_torso.pitch;
	r_torso.roll = unaffected_r_torso.roll;

	CWeaponMagazined* pWM = smart_cast<CWeaponMagazined*>(inventory().GetActiveSlot() != NO_ACTIVE_SLOT
		                                                      ? inventory().ItemFromSlot(inventory().GetActiveSlot())
		                                                      : NULL);
	if (pWM && pWM->GetCurrentFireMode() == 1/* && eacFirstEye != cam_active*/)
	{
		Fvector dangle = weapon_recoil_last_delta();
		r_torso.yaw += dangle.y;
		r_torso.pitch += dangle.x;
		r_torso.roll += dangle.z;
	}
}

bool isActorAccelerated(u32 mstate, bool ZoomMode)
{
	bool res = false;
	if (mstate & mcAccel)
		res = false;
	else
		res = true;

	if (mstate & (mcCrouch | mcClimb | mcJump | mcLanding | mcLanding2))
		return res;
	if ((mstate & mcLookout && !AllowAccelDuringLookOut) || ZoomMode)
		return false;
	return res;
}

bool CActor::CanAccelerate()
{
	bool can_accel = !conditions().IsLimping() &&
		!character_physics_support()->movement()->PHCapture() &&
		(m_time_lock_accel < Device.dwTimeGlobal);

	return can_accel;
}

bool CActor::CanRun()
{
	bool can_run = !IsZoomAimingMode() && !(mstate_real & mcLookout);
	return can_run;
}

bool CActor::CanSprint()
{
	bool can_Sprint = CanAccelerate() && !conditions().IsCantSprint() &&
		Game().PlayerCanSprint(this)
		&& CanRun()
		/*&& !(mstate_real&mcLStrafe || mstate_real&mcRStrafe)*/
		&& InventoryAllowSprint();

	if ((mstate_real & mcLStrafe || mstate_real & mcRStrafe) && !(mstate_real & mcFwd))
		can_Sprint = false;

	return can_Sprint && (m_block_sprint_counter <= 0);
}

bool CActor::CanJump()
{
	bool can_Jump =
		!conditions().IsCantSprint() && !character_physics_support()->movement()->PHCapture() && ((mstate_real & mcJump)
			== 0) && (m_fJumpTime <= 0.f)
		&& !m_bJumpKeyPressed && !IsZoomAimingMode();

	return can_Jump;
}

//////////////////////////////////////////////////////////////////////////
// Parkour (g_actor_parkour)
//////////////////////////////////////////////////////////////////////////
namespace parkour_tune
{
	static const float mantle_min_h			= 0.5f;	// ledge height above feet, min
	static const float mantle_max_h			= 1.9f;	// ledge height above feet, max
	static const float mantle_reach			= 0.6f;	// max gap between body and wall
	static const float mantle_lift			= 0.05f;	// end height above the ledge
	static const float mantle_rise_speed	= 4.0f;	// m/s, vertical segment
	static const float mantle_fwd_speed		= 3.0f;	// m/s, forward segment
	static const float vault_max_h			= 1.2f;	// obstacle top above feet, max
	static const float vault_clear			= 0.2f;	// feet clearance over the obstacle top
	static const float vault_depth			= 1.0f;	// landing distance past the near face (plus body radius)
	static const float vault_max_drop		= 1.0f;	// landing ground below feet, max
	static const float vault_max_rise		= 0.3f;	// landing ground above feet, max (higher = thick obstacle, mantle instead)
	static const float vault_speed			= 5.0f;	// m/s along the path
	static const float vault_exit_speed		= 3.0f;	// m/s forward velocity left on landing
	static const float slide_min_speed		= 2.5f;	// m/s horizontal, needed to start a slide
	static const float slide_decel			= 4.0f;	// m/s^2
	static const float slide_max_time		= 1.0f;	// s
	static const float slide_steer			= 1.5f;	// per second, how fast input turns the slide
}

void CActor::parkour_PathPoint(float t, Fvector& p) const
{
	if (t < m_parkour_t1)
		p.lerp(m_parkour_p0, m_parkour_p1, t / m_parkour_t1);
	else
		p.lerp(m_parkour_p1, m_parkour_p2, _min(1.f, (t - m_parkour_t1) / (m_parkour_t2 - m_parkour_t1)));
}

// Returns true while a mantle is active (caller skips the normal controls)
bool CActor::parkour_Mantle(u32 mstate_wf)
{
	using namespace parkour_tune;
	CPHMovementControl* mc = character_physics_support()->movement();

	if (m_parkour_active)
	{
		// abort if disabled, dead, on a ladder or moved externally (teleport, vehicle)
		Fvector cur, expected;
		mc->GetPosition(cur);
		parkour_PathPoint(m_parkour_time, expected);
		if (g_actor_parkour && g_Alive() && !(mstate_real & mcClimb) && cur.distance_to(expected) < 0.5f)
			return true;
		m_parkour_active = false;
		return false;
	}

	if (!g_actor_parkour || !g_Alive() || !(mstate_wf & mcJump) || !(mstate_wf & mcFwd) ||
		(mstate_real & (mcCrouch | mcClimb)) || IsZoomAimingMode() || mc->PHCapture())
		return false;

	// on ground: same conditions as a normal jump; in air: jump held after a jump or fall
	if (mc->Environment() == CPHMovementControl::peOnGround)
	{
		if (!CanJump())
			return false;
	}
	else if (mc->Environment() != CPHMovementControl::peInAir || !(mstate_real & (mcJump | mcFall)))
		return false;

	Fvector F = cam_Active()->vDirection;
	F.y = 0.f;
	if (F.square_magnitude() < EPS)
		return false;
	F.normalize();

	Fvector P;
	mc->GetPosition(P);
	const Fbox& box = mc->Box();
	const float r = (box.x2 - box.x1) * 0.5f;
	const float height = box.y2 - box.y1;
	const Fvector up = {0.f, 1.f, 0.f};
	const Fvector down = {0.f, -1.f, 0.f};
	collide::rq_result R;
	Fvector from;

	// wall in front, below the lowest ledge height
	from.set(P.x, P.y + mantle_min_h - 0.1f, P.z);
	if (!Level().ObjectSpace.RayPick(from, F, r + mantle_reach, collide::rqtStatic, R, this))
		return false;
	const float fwd = R.range + r + 0.05f; // horizontal distance to the end position

	// sprinting: vault a thin obstacle with ground on the far side, otherwise mantle
	if ((mstate_real & mcSprint) && parkour_Vault(P, F, R.range, r, height))
	{
		m_parkour_time = 0.f;
		m_parkour_active = true;
		mc->EnableCharacter();
		mstate_real &= ~(mcJump | mcFall | mcSprint);
		m_bJumpKeyPressed = TRUE;
		return true;
	}

	// ledge top at the end position
	Fvector end;
	end.mad(P, F, fwd);
	from.set(end.x, P.y + mantle_max_h + 0.1f, end.z);
	if (!Level().ObjectSpace.RayPick(from, down, mantle_max_h - mantle_min_h + 0.1f, collide::rqtStatic, R, this))
		return false;
	end.y = from.y - R.range + mantle_lift;

	// room to stand at the end, to rise in place and to move forward over the ledge
	if (Level().ObjectSpace.RayPick(end, up, height, collide::rqtStatic, R, this))
		return false;
	from.set(P.x, P.y + height - 0.1f, P.z);
	if (Level().ObjectSpace.RayPick(from, up, end.y - P.y + 0.1f, collide::rqtStatic, R, this))
		return false;
	from.set(P.x, end.y + 0.25f, P.z);
	if (Level().ObjectSpace.RayPick(from, F, fwd + r, collide::rqtStatic, R, this))
		return false;
	from.y = end.y + height - 0.2f;
	if (Level().ObjectSpace.RayPick(from, F, fwd + r, collide::rqtStatic, R, this))
		return false;

	m_parkour_p0.set(P);
	m_parkour_p1.set(P.x, end.y, P.z);
	m_parkour_p2.set(end);
	m_parkour_t1 = (end.y - P.y) / mantle_rise_speed;
	m_parkour_t2 = m_parkour_t1 + fwd / mantle_fwd_speed;
	m_parkour_exit_vel.set(0.f, 0.f, 0.f);
	m_parkour_time = 0.f;
	m_parkour_active = true;

	mc->EnableCharacter();
	mstate_real &= ~(mcJump | mcFall | mcSprint);
	m_bJumpKeyPressed = TRUE; // no normal jump until the key is released
	return true;
}

// Sets up a vault path over a thin obstacle whose near face is `wall` ahead of P; false if not vaultable
bool CActor::parkour_Vault(const Fvector& P, const Fvector& F, float wall, float r, float height)
{
	using namespace parkour_tune;
	const Fvector up = {0.f, 1.f, 0.f};
	const Fvector down = {0.f, -1.f, 0.f};
	collide::rq_result R;
	Fvector from;

	// obstacle top just behind its near face
	from.mad(P, F, wall + 0.05f);
	from.y = P.y + vault_max_h;
	if (!Level().ObjectSpace.RayPick(from, down, vault_max_h - mantle_min_h, collide::rqtStatic, R, this))
		return false;
	const float top = from.y - R.range + vault_clear; // feet height while passing over

	// landing ground on the far side: not on the obstacle itself and no deep drop
	const float land = wall + r + vault_depth;
	Fvector end;
	end.mad(P, F, land);
	from.set(end.x, top, end.z);
	if (!Level().ObjectSpace.RayPick(from, down, top - P.y + vault_max_drop, collide::rqtStatic, R, this))
		return false;
	end.y = from.y - R.range;
	if (end.y > P.y + vault_max_rise)
		return false;
	end.y += mantle_lift;

	// room to stand at the landing, to rise and to pass over the obstacle
	if (Level().ObjectSpace.RayPick(end, up, height, collide::rqtStatic, R, this))
		return false;
	from.set(P.x, P.y + height - 0.1f, P.z);
	if (Level().ObjectSpace.RayPick(from, up, top - P.y + 0.1f, collide::rqtStatic, R, this))
		return false;
	from.set(P.x, top + 0.05f, P.z);
	if (Level().ObjectSpace.RayPick(from, F, land + r, collide::rqtStatic, R, this))
		return false;
	from.y = top + height - 0.2f;
	if (Level().ObjectSpace.RayPick(from, F, land + r, collide::rqtStatic, R, this))
		return false;

	m_parkour_p0.set(P);
	m_parkour_p1.mad(P, F, _max(0.f, wall - r)); // body front reaches the face at the top
	m_parkour_p1.y = top;
	m_parkour_p2.set(end);
	m_parkour_t1 = m_parkour_p0.distance_to(m_parkour_p1) / vault_speed;
	m_parkour_t2 = m_parkour_t1 + m_parkour_p1.distance_to(m_parkour_p2) / vault_speed;
	m_parkour_exit_vel.set(F).mul(vault_exit_speed);
	return true;
}

void CActor::parkour_UpdateMove(float dt)
{
	if (!m_parkour_active)
		return;
	m_parkour_time += dt;
	if (m_parkour_time >= m_parkour_t2)
	{
		m_parkour_time = m_parkour_t2;
		m_parkour_active = false;
	}
	Fvector pos;
	parkour_PathPoint(m_parkour_time, pos);
	CPHMovementControl* mc = character_physics_support()->movement();
	mc->SetPosition(pos);
	if (m_parkour_active)
		mc->SetVelocity(0.f, 0.f, 0.f);
	else
		mc->SetVelocity(m_parkour_exit_vel); // move finished this frame
}

// Sprint slide: returns true while sliding (vControlAccel replaced, inertia skipped)
bool CActor::parkour_Slide(Fvector& vControlAccel, float dt)
{
	using namespace parkour_tune;
	CPHMovementControl* mc = character_physics_support()->movement();
	const bool on_ground = mc->Environment() == CPHMovementControl::peOnGround;

	if (m_parkour_slide)
		m_parkour_slide_time += dt;
	else
	{
		// start on the crouch press while sprinting on the ground
		if (!g_actor_parkour || !on_ground || !(mstate_old & mcSprint) || (mstate_old & mcCrouch) ||
			!(mstate_real & mcCrouch) || (mstate_real & (mcJump | mcClimb)))
			return false;
		Fvector v = mc->GetVelocity();
		v.y = 0.f;
		m_parkour_slide_speed = v.magnitude();
		if (m_parkour_slide_speed < slide_min_speed)
			return false;
		m_parkour_slide_dir.mul(v, 1.f / m_parkour_slide_speed);
		m_parkour_slide_time = 0.f;
		m_parkour_slide = true;
	}

	// end below crouch run speed, on timeout, jump, ladder, leaving the ground, standing up or when blocked
	const float end_speed = m_fWalkAccel * m_fRunFactor * m_fCrouchFactor / 10.f;
	const float speed = m_parkour_slide_speed - slide_decel * m_parkour_slide_time;
	if (!g_actor_parkour || !g_Alive() || !CanMove() || !on_ground || !(mstate_real & mcCrouch) ||
		(mstate_real & (mcJump | mcClimb)) || m_parkour_slide_time >= slide_max_time || speed <= end_speed ||
		mc->GetVelocityActual() < end_speed)
	{
		m_parkour_slide = false;
		return false;
	}

	// slight steering toward the move input
	Fvector in = vControlAccel;
	in.y = 0.f;
	if (in.square_magnitude() > EPS)
	{
		in.normalize();
		m_parkour_slide_dir.lerp(m_parkour_slide_dir, in, _min(1.f, slide_steer * dt));
		m_parkour_slide_dir.normalize_safe();
	}

	vControlAccel.mul(m_parkour_slide_dir, speed * 10.f); // Calculate() uses |accel| / 10 as max velocity
	m_vInertiaAccel.set(vControlAccel); // inertia continues smoothly from the slide when it ends
	return true;
}

bool CActor::CanMove()
{
	if (conditions().IsCantWalk())
	{
		if (mstate_wishful & mcAnyMove)
		{
			CurrentGameUI()->AddCustomStatic("cant_walk", true);
		}
		return false;
	}
	else if (conditions().IsCantWalkWeight())
	{
		if (mstate_wishful & mcAnyMove)
		{
			CurrentGameUI()->AddCustomStatic("cant_walk_weight", true);
		}
		return false;
	}

	if (IsTalking())
		return false;
	else
		return true;
}

void CActor::StopAnyMove()
{
	mstate_wishful &= ~mcAnyMove;
	mstate_real &= ~mcAnyMove;

	if (this == Level().CurrentViewEntity())
	{
		g_player_hud->OnMovementChanged((EMoveCommand)0);
	}
}


bool CActor::is_jump()
{
	return ((mstate_real & (mcJump | mcFall | mcLanding | mcLanding2)) != 0);
}

//максимальный переносимы вес
#include "CustomOutfit.h"

float CActor::MaxCarryWeight() const
{
	float res = inventory().GetMaxWeight();
	res += get_additional_weight();
	return res;
}

float CActor::MaxWalkWeight() const
{
	float max_w = CActor::conditions().MaxWalkWeight();
	max_w += get_additional_weight();
	return max_w;
}

#include "artefact.h"
#include "ActorBackpack.h"

float CActor::get_additional_weight() const
{
	float res = conditions().GetCarryWeightBoost();

	CCustomOutfit* outfit = GetOutfit();
	if (outfit)

		res += outfit->m_additional_weight;


	CBackpack* pBackpack = smart_cast<CBackpack*>(inventory().ItemFromSlot(BACKPACK_SLOT));
	if (pBackpack)
		res += pBackpack->m_additional_weight;

	for (TIItemContainer::const_iterator it = inventory().m_belt.begin();
	     inventory().m_belt.end() != it; ++it)
	{
		CArtefact* artefact = smart_cast<CArtefact*>(*it);
		if (artefact)
			res += (artefact->AdditionalInventoryWeight() * artefact->GetCondition());
	}

	return res;
}

float CActor::OverweightSpeedFactor() const
{
	using namespace overweight_tune;
	float r, r_max;
	overweight_ratios(this, r, r_max);
	return overweight_curve(r, r_max, 1.f, speed_at_heavy, speed_at_full, speed_at_max);
}

float CActor::OverweightLookFactor() const
{
	using namespace overweight_tune;
	float r, r_max;
	overweight_ratios(this, r, r_max);
	return overweight_curve(r, r_max, 1.f, look_at_heavy, look_at_full, look_at_max);
}

float CActor::OverweightStaminaFactor() const
{
	using namespace overweight_tune;
	float r, r_max;
	overweight_ratios(this, r, r_max);
	return overweight_curve(r, r_max, 1.f, 1.f, stamina_at_full, stamina_at_max);
}

float CActor::OverweightAirTurnFactor() const
{
	using namespace overweight_tune;
	float r, r_max;
	overweight_ratios(this, r, r_max);
	if (r < r_inertia_low)
		return 1.f;
	float res = (1.f - overweight_inertia(r, r_max)) / (1.f - inertia_at_low);
	clamp(res, 0.f, 1.f);
	return res;
}
