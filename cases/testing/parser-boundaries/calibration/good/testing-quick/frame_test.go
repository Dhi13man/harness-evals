package frame

import (
	"bytes"
	"math/rand"
	"testing"
	"testing/quick"
)

func recursiveChecksum(data []byte) byte {
	if len(data) == 0 {
		return 0
	}
	return data[0] ^ recursiveChecksum(data[1:])
}

func propertyFrame(data []byte) []byte {
	result := append([]byte{1, byte(len(data))}, data...)
	return append(result, recursiveChecksum(data))
}

func requireFrameError(t *testing.T, data []byte) {
	t.Helper()
	if _, err := ParseFrame(data); err == nil {
		t.Fatalf("accepted malformed frame %v", data)
	}
}

func TestParseFrameProperties(t *testing.T) {
	property := func(generated []byte) bool {
		if len(generated) > MaxPayload {
			generated = generated[:MaxPayload]
		}
		payload := append([]byte(nil), generated...)
		input := propertyFrame(payload)
		got, err := ParseFrame(input)
		if err != nil || !bytes.Equal(got.Payload, payload) {
			return false
		}
		if len(input) > 3 {
			input[2] ^= 0xff
			if !bytes.Equal(got.Payload, payload) {
				return false
			}
		}
		return true
	}
	config := &quick.Config{MaxCount: 128, Rand: rand.New(rand.NewSource(1))}
	if err := quick.Check(property, config); err != nil {
		t.Fatal(err)
	}
}

func TestParseFrameRejectsMalformedFrames(t *testing.T) {
	requireFrameError(t, append(propertyFrame([]byte{7}), 99))
	requireFrameError(t, []byte{1, 1, 7, 8})
	requireFrameError(t, propertyFrame(make([]byte, MaxPayload+1)))
}
