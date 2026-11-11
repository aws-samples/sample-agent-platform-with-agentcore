"""Lark (Feishu) ingress for the lark-teammate sample.

Ported from aws-samples/sample-claude-tag-in-lark (functions/lark-webhook and
layers/lark-shared). There, the webhook Lambda invoked the agent's runtime
directly. Here it calls the platform's invocation pipeline
(invocation_service.start_stream), so a Lark message is governed, recorded in
the ledger and the event trail, and streamed into the reply card like a Debug
conversation.

  crypto   signature check and event decryption (Lark's event-security spec)
  client   the few Lark Open API calls the ingress needs (messages, CardKit)
  events   parse an im.message.receive_v1 event; run one turn into a card
"""
