# Deployment artifacts as a single-layer OCI image (no OS, not runnable). Build context: build/
# The CloudFormation "ArtifactCopier" custom resource downloads layers[0] from the registry and extracts it into S3.
FROM scratch
COPY artifacts/ /
LABEL org.opencontainers.image.title="deepresearch-artifacts" \
      org.opencontainers.image.description="Lambda zip, AgentCore Runtime code zip, Gateway tool schema and harness skill"
