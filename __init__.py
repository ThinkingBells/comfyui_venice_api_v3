import os

from comfy_api.latest import ComfyExtension, io

from .nodes.gen_text_node import GenerateText
from .pyserver import routes  # registers /veniceai/save_apikey and /veniceai/get_apikey


class VeniceExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            GenerateText,
        ]


async def comfy_entrypoint() -> VeniceExtension:
    return VeniceExtension()


WEB_DIRECTORY = os.path.join(os.path.dirname(__file__), "js")

__all__ = ["WEB_DIRECTORY"]
