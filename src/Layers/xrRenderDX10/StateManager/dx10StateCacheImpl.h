#ifndef	dx10StateCacheImpl_included
#define	dx10StateCacheImpl_included
#pragma once

#include "../dx10StateUtils.h"

using dx10StateUtils::operator==;

template <class IDeviceState, class StateDecs>
IDeviceState*
dx10StateCache<IDeviceState, StateDecs>
::GetState(SimulatorStates& state_code)
{
	StateDecs desc;
	dx10StateUtils::ResetDescription(desc);
	state_code.UpdateDesc(desc);

	dx10StateUtils::ValidateState(desc);

	//	Shader creation path (may run off the render thread): never touches the memo
	bool bFound;
	return FindOrCreateState(desc, bFound);

	/*
	u32 crc = crc32( &desc, sizeof(desc));

	pResult = FindState( desc, crc);

	if (!pResult)
	{
		StateRecord rec;
		rec.m_crc = crc;
		CreateState(desc, &rec.m_pState);
		pResult = rec.m_pState;
		m_StateArray.push_back(rec);
	}

	return pResult;
	*/
}

template <class IDeviceState, class StateDecs>
IDeviceState*
dx10StateCache<IDeviceState, StateDecs>
::GetState(StateDecs& desc)
{
	dx10StateUtils::ValidateState(desc);

	//	Records are append-only, so the same bytes as the last found desc find the same first record
	if (m_pLastState && !memcmp(&m_LastDesc, &desc, sizeof(desc)))
		return m_pLastState;

	bool bFound;
	IDeviceState* pResult = FindOrCreateState(desc, bFound);

	if (bFound)
	{
		CopyMemory(&m_LastDesc, &desc, sizeof(desc));
		m_pLastState = pResult;
	}

	return pResult;
}

template <class IDeviceState, class StateDecs>
IDeviceState*
dx10StateCache<IDeviceState, StateDecs>
::FindOrCreateState(const StateDecs& desc, bool& bFound)
{
	IDeviceState* pResult;

	u32 crc = dx10StateUtils::GetHash(desc);

	pResult = FindState(desc, crc);
	bFound = (pResult != NULL);

	if (!pResult)
	{
		StateRecord rec;
		rec.m_crc = crc;
		CreateState(desc, &rec.m_pState);
		//	Store what GetDesc reports (FindState used to call it per candidate); the object is immutable
		if (rec.m_pState)
			rec.m_pState->GetDesc(&rec.m_desc);
		else
			rec.m_desc = desc;
		pResult = rec.m_pState;
		m_StateArray.push_back(rec);
	}

	return pResult;
}

template <class IDeviceState, class StateDecs>
IDeviceState*
dx10StateCache<IDeviceState, StateDecs>
::FindState(const StateDecs& desc, u32 StateCRC)
{
	u32 res = 0xffffffff;
	for (u32 i = 0; i < m_StateArray.size(); ++i)
	{
		if (m_StateArray[i].m_crc == StateCRC)
		{
			//if ( !memcmp(&descCandidate, &desc, sizeof(desc)) )
			if (m_StateArray[i].m_desc == desc)
				//break;
				//	TEST
			{
				res = i;
				break;
			}
			//else
			//{
			//	VERIFY(0);
			//}
		}
	}

	if (res != 0xffffffff)
		return m_StateArray[res].m_pState;
	else
		return NULL;
	/*
	if (i!=m_StateArray.size())
		return m_StateArray[i].m_pState;
	else
		return NULL;
	    */
}

#endif	//	dx10StateCacheImpl_included
