#ifndef	dx10ConstantBuffer_impl_included
#define	dx10ConstantBuffer_impl_included
#pragma once

IC Fvector4* dx10ConstantBuffer::Access(u16 offset)
{
	//	Check buffer size in client code: don't know if actual data will cross
	//	buffer boundaries.
	VERIFY(offset<(int)m_uiBufferSize);
	BYTE* res = ((BYTE*)m_pBufferData) + offset;
	return (Fvector4*)res;
}

IC void dx10ConstantBuffer::Update(void* dst, const void* src, u32 size)
{
	//	Byte compare, not float compare: NaN and -0.0 must count as changes
	if (memcmp(dst, src, size))
	{
		CopyMemory(dst, src, size);
		m_bChanged = true;
	}
}

IC void dx10ConstantBuffer::set(R_constant* C, R_constant_load& L, const Fmatrix& A)
{
	VERIFY(RC_float == C->type);
	//	TEST
	//return;

	//Fvector4*	it	= c_f.access	(L.index);
	Fvector4* it = Access(L.index);
	Fvector4 M[4];
	M[0].set(A._11, A._21, A._31, A._41);
	M[1].set(A._12, A._22, A._32, A._42);
	M[2].set(A._13, A._23, A._33, A._43);
	M[3].set(A._14, A._24, A._34, A._44);
	u32 rows = 0;
	switch (L.cls)
	{
	case RC_2x4:
		rows = 2;
		break;
	case RC_3x4:
		rows = 3;
		break;
	case RC_4x4:
		rows = 4;
		break;
	default:
#ifdef DEBUG
		Debug.fatal		(DEBUG_INFO,"Invalid constant run-time-type for '%s'",*C->name);
#else
		NODEFAULT;
#endif
	}
	VERIFY(u32((u32)L.index+rows*lineSize) <= m_uiBufferSize);
	Update(it, M, rows * lineSize);
}

IC void dx10ConstantBuffer::set(R_constant* C, R_constant_load& L, const Fvector4& A)
{
	VERIFY(RC_float == C->type);
	VERIFY(RC_1x4 == L.cls || RC_1x3 == L.cls || RC_1x2 == L.cls || RC_1x1 == L.cls);
	//Fvector4*	it	= Access(L.index);
	//it->set	(A);

	float* it = (float*)Access(L.index);

	size_t count = 4;
	switch (L.cls)
	{
	case RC_1x1:
		count = 1;
		break;
	case RC_1x2:
		count = 2;
		break;
	case RC_1x3:
		count = 3;
		break;
	case RC_1x4:
		count = 4;
		break;
	default:
		break;
	}

	VERIFY(u32((u32)L.index+count*sizeof(float)) <= m_uiBufferSize);
	Update(it, &A[0], u32(count*sizeof(float)));

	//c_f.access	(L.index)->set	(A);
	//c_f.dirty	(L.index,L.index+1);
}

IC void dx10ConstantBuffer::set(R_constant* C, R_constant_load& L, float A)
{
	VERIFY(RC_float == C->type);
	VERIFY(RC_1x1 == L.cls);
	float* it = (float*)Access(L.index);
	VERIFY(u32((u32)L.index+sizeof(float)) <= m_uiBufferSize);
	Update(it, &A, sizeof(float));

	//c_f.access	(L.index)->set	(A);
	//c_f.dirty	(L.index,L.index+1);
}

IC void dx10ConstantBuffer::set(R_constant* C, R_constant_load& L, int A)
{
	VERIFY(RC_int == C->type);
	VERIFY(RC_1x1 == L.cls);
	int* it = (int*)Access(L.index);
	VERIFY(u32((u32)L.index+sizeof(int)) <= m_uiBufferSize);
	Update(it, &A, sizeof(int));

	//c_f.access	(L.index)->set	(A);
	//c_f.dirty	(L.index,L.index+1);
}

IC void dx10ConstantBuffer::seta(R_constant* C, R_constant_load& L, u32 e, const Fmatrix& A)
{
	//	TEST
	//return;
	VERIFY(RC_float == C->type);
	u32 rows = 0;
	switch (L.cls)
	{
	case RC_2x4:
		rows = 2;
		break;
	case RC_3x4:
		rows = 3;
		break;
	case RC_4x4:
		rows = 4;
		break;
	default:
#ifdef DEBUG
		Debug.fatal		(DEBUG_INFO,"Invalid constant run-time-type for '%s'",*C->name);
#else
		NODEFAULT;
#endif
	}
	u32 base = (u32)L.index + rows * lineSize * e;
	Fvector4* it = Access((u16)base);
	VERIFY((base+rows*lineSize) <= m_uiBufferSize);
	Fvector4 M[4];
	M[0].set(A._11, A._21, A._31, A._41);
	M[1].set(A._12, A._22, A._32, A._42);
	M[2].set(A._13, A._23, A._33, A._43);
	M[3].set(A._14, A._24, A._34, A._44);
	Update(it, M, rows * lineSize);
}

IC void dx10ConstantBuffer::seta(R_constant* C, R_constant_load& L, u32 e, const Fvector4& A)
{
	//	TEST
	//return;
	VERIFY(RC_float == C->type);
	VERIFY(RC_1x4 == L.cls || RC_1x3 == L.cls || RC_1x2 == L.cls);

	static const u16 lineSize = 4 * sizeof(float);
	u32 base = (u32)L.index + lineSize * e;
	Fvector4* it = Access((u16)base);
	VERIFY((base+lineSize) <= m_uiBufferSize);
	Update(it, &A, lineSize);

	//u32			base	= L.index + e;
	//c_f.access	(base)->set	(A);
	//c_f.dirty	(base,base+1);
}

IC void dx10ConstantBuffer::seta(R_constant* C, R_constant_load& L, u32 e, const Fvector4* A, u32 count)
{
	VERIFY(RC_float == C->type);
	VERIFY(RC_1x4 == L.cls || RC_1x3 == L.cls || RC_1x2 == L.cls);

	u32 base = (u32)L.index + lineSize * e;
	Fvector4* it = Access((u16)base);
	VERIFY((base+lineSize*count) <= m_uiBufferSize);
	Update(it, A, lineSize * count);
}

IC void dx10ConstantBuffer::set_bytes(R_constant_load& L, const void* data, u32 bytes)
{
	VERIFY((u32)L.index + L.size <= m_uiBufferSize);
	BYTE* it = (BYTE*)Access(L.index);
	const u32 copied = _min(bytes, L.size);
	bool changed = memcmp(it, data, copied) != 0;
	for (u32 i = copied; !changed && i < L.size; ++i)
		changed = it[i] != 0;
	if (changed)
	{
		CopyMemory(it, data, copied);
		ZeroMemory(it + copied, L.size - copied);
		m_bChanged = true;
	}
}

IC void* dx10ConstantBuffer::AccessDirect(R_constant_load& L, u32 DataSize)
{
	//	Check buffer size in client code: don't know if actual data will cross
	//	buffer boundaries.
	VERIFY(L.index<(int)m_uiBufferSize);
	BYTE* res = ((BYTE*)m_pBufferData) + L.index;

	if ((u32)L.index + DataSize <= m_uiBufferSize)
	{
		m_bChanged = true;
		return res;
	}
	else return 0;
}

#endif	//	dx10ConstantBuffer_impl_included
