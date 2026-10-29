#ifndef DEMETER_PLACEMENT_HASHMAP_H
#define DEMETER_PLACEMENT_HASHMAP_H
#include <linux/types.h>

#include "vector.h"
#include "cwisstable.h"

#define CHECK_INSERTED(ins, success, ...)                              \
	({                                                             \
		HashMapU64U64_Insert __insert = (ins);                 \
		HashMapU64U64_Entry *__entry =                         \
			HashMapU64U64_Iter_get(&__insert.iter);        \
		CWISS_CHECK(__insert.inserted == (success) && __entry, \
			    __VA_ARGS__);                              \
		__entry;                                               \
	})


// HashMapU64U64: A hash map from u64 to u64
CWISS_DECLARE_FLAT_HASHMAP(HashMapU64U64, u64, u64);
noinline static inline HashMapU64U64_Entry *
HashMapU64U64_update(HashMapU64U64 *self, HashMapU64U64_Entry const *e)
{
	HashMapU64U64_Entry *r = CHECK_INSERTED(
		HashMapU64U64_insert(self, e), false,
		"cannot update a non-existant entry key=%llx val=%llu", e->key,
		e->val);
	r->val = e->val;
	return r;
}

noinline static inline void HashMapU64U64_swap(HashMapU64U64 *self,
					       u64 const *keyx, u64 const *keyy)
{
	HashMapU64U64_Iter iterx = HashMapU64U64_find(self, keyx);
	HashMapU64U64_Entry *x = HashMapU64U64_Iter_get(&iterx);
	CWISS_CHECK(
		x != NULL,
		"getting a non-existant entry via keyx from the map keyx=0x%llx keyy=0x%llx",
		*keyx, *keyy);

	HashMapU64U64_Iter itery = HashMapU64U64_find(self, keyy);
	HashMapU64U64_Entry *y = HashMapU64U64_Iter_get(&itery);
	CWISS_CHECK(
		y != NULL,
		"getting a non-existant entry via keyy from the map keyx=0x%llx keyy=0x%llx",
		*keyx, *keyy);

	swap(x->val, y->val);
}
noinline static inline HashMapU64U64_Entry *
HashMapU64U64_get_or_insert(HashMapU64U64 *map, u64 key, u64 val)
{
	HashMapU64U64_Iter iter = HashMapU64U64_find(map, &key);
	HashMapU64U64_Entry *e = HashMapU64U64_Iter_get(&iter);
	if (e != NULL) {
		return e;
	}
	HashMapU64U64_Entry ne = { key, val };
	return CHECK_INSERTED(HashMapU64U64_insert(map, &ne), true,
			      "insertion failed key=%llx val=%llu", key, val);
}

#endif // DEMETER_PLACEMENT_HASHMAP_H
