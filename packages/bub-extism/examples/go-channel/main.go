package main

import (
	"encoding/json"
	"fmt"

	"github.com/extism/go-pdk"
)

type request struct {
	Hook string         `json:"hook"`
	Args map[string]any `json:"args"`
}

type response struct {
	Value any  `json:"value,omitempty"`
	Skip  bool `json:"skip,omitempty"`
	Error any  `json:"error,omitempty"`
}

//go:wasmexport provide_channels
func provideChannels() int32 {
	return outputJSON(response{
		Value: []map[string]any{
			{
				"name":                "go-echo",
				"pollIntervalSeconds": 1,
				"functions": map[string]string{
					"send": "channel_send",
				},
			},
		},
	})
}

//go:wasmexport channel_send
func channelSend() int32 {
	var req request
	if err := pdk.InputJSON(&req); err != nil {
		return outputError(err)
	}
	message, _ := req.Args["message"].(map[string]any)
	content, _ := message["content"].(string)
	return outputJSON(response{
		Value: map[string]any{
			"ok":      true,
			"channel": "go-echo",
			"sent":    content,
		},
	})
}

func outputJSON(value any) int32 {
	if err := pdk.OutputJSON(value); err != nil {
		return outputError(err)
	}
	return 0
}

func outputError(err error) int32 {
	pdk.SetErrorString(fmt.Sprintf("go-channel: %v", err))
	encoded, _ := json.Marshal(response{
		Error: map[string]string{
			"message": err.Error(),
		},
	})
	pdk.Output(encoded)
	return 1
}

func main() {}
