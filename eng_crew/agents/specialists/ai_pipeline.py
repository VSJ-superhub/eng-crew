from .base_specialist import SpecialistAgent


class AIPipelineAgent(SpecialistAgent):
    agent_type = "ai_pipeline"
    file_extensions: list[str] = [".py"]
