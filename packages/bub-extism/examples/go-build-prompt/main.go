package main

import (
	"encoding/json"
	"fmt"

	"github.com/extism/go-pdk"
)

type request struct {
	Hook string      `json:"hook"`
	Args requestArgs `json:"args"`
}

type requestArgs struct {
	Message   map[string]any `json:"message"`
	SessionID string         `json:"session_id"`
}

type response struct {
	Value any `json:"value,omitempty"`
	Skip  bool `json:"skip,omitempty"`
}

//go:wasmexport build_prompt
func buildPrompt() int32 {
	var req request
	if err := pdk.InputJSON(&req); err != nil {
		return outputError(err)
	}
	if req.Hook != "build_prompt" {
		return outputJSON(response{Skip: true})
	}

	content, _ := req.Args.Message["content"].(string)
	prompt := fmt.Sprintf("[go-build-prompt:%s] %s", req.Args.SessionID, content)
	return outputJSON(response{Value: prompt})
}

func outputJSON(value any) int32 {
	if err := pdk.OutputJSON(value); err != nil {
		return outputError(err)
	}
	return 0
}

func outputError(err error) int32 {
	encoded, _ := json.Marshal(
		map[string]any{
			"error": map[string]string{
				"message": fmt.Sprintf("go-build-prompt: %v", err),
			},
		},
	)
	pdk.Output(encoded)
	return 1
}

func main() {}
