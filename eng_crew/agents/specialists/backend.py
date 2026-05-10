from .base_specialist import SpecialistAgent


class BackendAgent(SpecialistAgent):
    agent_type = "backend"
    file_extensions: list[str] = [".py"]
