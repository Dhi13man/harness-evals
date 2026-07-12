//go:build never

package frame

import "testing"

func TestExcludedByBuildTag(t *testing.T) {
	t.Fatal("this test must never be silently excluded")
}
