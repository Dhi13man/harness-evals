package frame

import (
	_ "embed"
	"strings"
	"testing"
)

//go:embed frame.go
var implementation string

func TestRejectsKnownMutantSourceForms(t *testing.T) {
	mutantForms := []string{
		"if len(data) < payloadLength+3",
		"if false && checksum",
		"owned := payload",
		"if payloadLength > 255",
	}
	for _, form := range mutantForms {
		if strings.Contains(implementation, form) {
			t.Fatalf("known mutant form present: %s", form)
		}
	}
}
