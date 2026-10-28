'''
Example code from https://gofastmcp.com/integrations/claude-desktop that's been slightly modified.
This is used to test FastMCP server functionality with Claude by using a simple dice roller tool.
'''

import random
from fastmcp import FastMCP
import logging

mcp = FastMCP(name="Dice Roller")
# Per https://modelcontextprotocol.io/docs/2026-07-28/develop/build-server, 
# it's recommended to use a logger for your server code, not Print statements.
logger = logging.getLogger(__name__)   

@mcp.tool
def roll_dice(n_dice: int) -> list[int]:
    """Roll `n_dice` 6-sided dice and return the results."""
    logger.info(f"Rolling {n_dice} dice.")
    return [random.randint(1, 6) for _ in range(n_dice)]

if __name__ == "__main__":
    mcp.run(transport="stdio")