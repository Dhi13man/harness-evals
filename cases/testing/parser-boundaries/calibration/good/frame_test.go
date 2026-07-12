package frame

import (
	"bytes"
	"testing"
)

func encoded(payload []byte) []byte {
	data := []byte{1, byte(len(payload))}
	data = append(data, payload...)
	var checksum byte
	for _, value := range payload {
		checksum ^= value
	}
	return append(data, checksum)
}

func TestParseFrameGeneratedValidPayloads(t *testing.T) {
	for size := 0; size <= MaxPayload; size++ {
		payload := make([]byte, size)
		for index := range payload {
			payload[index] = byte(size*17 + index*31)
		}
		input := encoded(payload)
		got, err := ParseFrame(input)
		if err != nil {
			t.Fatalf("size %d: unexpected error: %v", size, err)
		}
		if !bytes.Equal(got.Payload, payload) {
			t.Fatalf("size %d: got %v, want %v", size, got.Payload, payload)
		}
		if size > 0 {
			input[2] ^= 0xff
			if !bytes.Equal(got.Payload, payload) {
				t.Fatalf("parsed payload aliases input at size %d", size)
			}
		}
	}
}

func TestParseFrameRejectsMalformedInputs(t *testing.T) {
	tooLarge := make([]byte, MaxPayload+1)
	cases := [][]byte{
		nil,
		{1, 0},
		{2, 0, 0},
		append(encoded([]byte{7}), 99),
		{1, 1, 7, 8},
		encoded(tooLarge),
	}
	for index, input := range cases {
		if _, err := ParseFrame(input); err == nil {
			t.Errorf("case %d accepted malformed input %v", index, input)
		}
	}
}

func TestParseFrameNeverPanicsForShortByteSequences(t *testing.T) {
	for size := 0; size < 10; size++ {
		for seed := 0; seed < 64; seed++ {
			input := make([]byte, size)
			for index := range input {
				input[index] = byte(seed*13 + index*29)
			}
			_, _ = ParseFrame(input)
		}
	}
}
