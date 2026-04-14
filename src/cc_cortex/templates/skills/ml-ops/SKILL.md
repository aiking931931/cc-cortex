---
name: ml-ops
description: ML model deployment, versioning, monitoring, experiment tracking. Triggers on "MLOps", "模型部署", "model serving", "experiment tracking", "ML pipeline", "model monitoring".
user-invocable: true
disable-model-invocation: true
---

# /ml-ops — ML Operations Patterns

I deploy models that are reproducible, monitorable, and rollbackable. A model without monitoring is a time bomb.

> **You MUST** version models with data hash + code hash + hyperparams.
> **You MUST** monitor prediction drift and data drift in production.
> **You MUST** keep training reproducible (seed, deps, data snapshot).

## Decision Tree

```
Serving?
  ├─ Real-time (<100ms) → FastAPI + GPU / TensorRT / vLLM
  ├─ Batch (hourly) → Scheduled job + object storage
  ├─ Edge → ONNX / TFLite + quantization
  └─ Prototype → Gradio / Streamlit

Experiment tracking?
  ├─ Solo → MLflow / W&B local
  ├─ Team → W&B cloud / MLflow server
  └─ Enterprise → Vertex AI / SageMaker
```

## Production Checklist

1. **Versioning**: Model artifact + training data hash + config
2. **A/B testing**: Shadow mode first, then canary rollout
3. **Monitoring**: Input distribution, prediction distribution, latency
4. **Rollback**: Previous model version always warm and ready
5. **Alerts**: Drift detection (KS test / PSI), latency spike, error rate
