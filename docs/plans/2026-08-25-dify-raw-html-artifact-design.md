# Dify Raw HTML Artifact Delivery Design

## Scope

This change applies only to `dify-proxy`. It does not change the OpenWebUI
proxy, either Strands Runtime implementation, Code Interpreter's bounded result
contract, S3 ownership layout, or non-HTML artifact behavior.

## Response contract

For every validated HTML artifact, the Dify response contains the complete
UTF-8 document in the frontend's existing Markdown contract:

````text
```html
<!DOCTYPE html>
<html lang="en">
...
</html>
```
````

The response does not expose the internal S3 URI or a presigned URL for HTML.
The frontend captures the fenced document, stores it on its secure server, and
serves it through its existing authenticated path.

## Authoritative source

The agent should continue generating the file in Code Interpreter, uploading it
to `s3://ah-dify/harness_dev/{user_id}/{conversation_id}/`, and returning the
internal `<agentcore-artifacts>` manifest. The proxy validates the bucket,
prefix, extension, object size, and exact user/conversation ownership tags
before downloading the object.

Validated S3 HTML is the only permitted dashboard source. If the model returns
a direct fenced HTML document, the proxy removes it. When a validated S3 HTML
artifact exists, the proxy emits that document. Otherwise it returns a clear
artifact-delivery error. The proxy logs `validated_s3` or
`direct_model_rejected` as the delivery outcome.

## Validation and streaming

Downloaded HTML must fit the existing artifact size limit, decode as strict
UTF-8 (an optional UTF-8 BOM is removed), begin with `<!DOCTYPE html>`, end with
`</html>`, and not contain a Markdown triple-backtick sequence that would break
the frontend fence. It embeds analysed data and custom CSS/JavaScript; a
standard Chart.js CDN script is permitted. Invalid S3 HTML is not emitted.

Normal answer text can continue streaming. A direct `html` fence is buffered
and suppressed so it can never reach the frontend. Tool and skill status events
remain live. Validated final HTML is emitted in bounded OpenAI-compatible SSE
chunks; non-streaming requests receive the same reconstructed content in one
completion.

Non-HTML artifacts retain the existing reference or presigned-link behavior.
If HTML and another artifact type are returned together, raw HTML fences are
emitted and the non-HTML artifacts use their existing representation.

## Failure behavior

If S3 validation or HTML loading fails, the proxy returns a clear error stating
that no validated Code Interpreter HTML artifact was produced. Direct model
HTML is never used as a fallback, and partially downloaded HTML is never
emitted.
