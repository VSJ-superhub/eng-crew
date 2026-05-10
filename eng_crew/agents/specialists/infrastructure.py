from .base_specialist import SpecialistAgent


class InfrastructureAgent(SpecialistAgent):
    agent_type = "infrastructure"
    file_extensions: list[str] = [".yml", ".yaml", ".toml", ".dockerfile"]
