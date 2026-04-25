use extism_pdk::{plugin_fn, FnResult};
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
struct Request {
    hook: String,
    args: Args,
}

#[derive(Deserialize)]
struct Args {
    prompt: Value,
    session_id: String,
}

#[plugin_fn]
pub fn run_model_stream(input: String) -> FnResult<String> {
    let request: Request = serde_json::from_str(&input)?;
    if request.hook != "run_model_stream" {
        return Ok(json!({ "skip": true }).to_string());
    }

    let prompt = match request.args.prompt {
        Value::String(value) => value,
        other => other.to_string(),
    };
    let text = format!("[rust-model-stream:{}] {}", request.args.session_id, prompt);

    Ok(json!({
        "value": {
            "events": [
                {
                    "kind": "text",
                    "data": { "delta": text }
                },
                {
                    "kind": "final",
                    "data": { "text": text }
                }
            ],
            "usage": {
                "output_tokens": text.split_whitespace().count()
            }
        }
    })
    .to_string())
}
