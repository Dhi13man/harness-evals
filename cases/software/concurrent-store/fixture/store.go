package counterstore

type Store struct {
	counts map[string]int
}

func NewStore() *Store {
	return &Store{counts: make(map[string]int)}
}

func (store *Store) Increment(key string) int {
	store.counts[key]++
	return store.counts[key]
}

func (store *Store) Get(key string) int {
	return store.counts[key]
}

func (store *Store) Transfer(from, to string, amount int) bool {
	if amount <= 0 || store.counts[from] < amount {
		return false
	}
	store.counts[from] -= amount
	store.counts[to] += amount
	return true
}

func (store *Store) Snapshot() map[string]int {
	return store.counts
}
