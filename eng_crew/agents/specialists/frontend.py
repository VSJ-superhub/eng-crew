from .base_specialist import SpecialistAgent


class FrontendAgent(SpecialistAgent):
    agent_type = "frontend"
    file_extensions: list[str] = [".tsx", ".ts", ".jsx", ".js", ".css"]
