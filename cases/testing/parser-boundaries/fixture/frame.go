package frame

import "errors"

const MaxPayload = 32

type Frame struct {
	Version byte
	Payload []byte
}

func ParseFrame(data []byte) (Frame, error) {
	if len(data) < 3 {
		return Frame{}, errors.New("frame too short")
	}
	if data[0] != 1 {
		return Frame{}, errors.New("unsupported version")
	}
	payloadLength := int(data[1])
	if payloadLength > MaxPayload {
		return Frame{}, errors.New("payload too large")
	}
	if len(data) != payloadLength+3 {
		return Frame{}, errors.New("length mismatch")
	}
	payload := data[2 : 2+payloadLength]
	var checksum byte
	for _, value := range payload {
		checksum ^= value
	}
	if checksum != data[len(data)-1] {
		return Frame{}, errors.New("checksum mismatch")
	}
	owned := append([]byte(nil), payload...)
	return Frame{Version: 1, Payload: owned}, nil
}
