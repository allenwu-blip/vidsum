"""The MCP server starts on stdio and lists exactly the two tools."""
import asyncio
import sys
import unittest
from mcp import Client
from mcp.client.stdio import StdioServerParameters


class McpServer(unittest.TestCase):
    def test_tools_over_stdio(self):
        async def check():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'vidsum.cli', 'mcp'])
            async with Client(params) as client:
                tools = await client.list_tools()
                return {t.name: t for t in tools.tools}
        tools = asyncio.run(check())
        self.assertEqual(set(tools), {'summarize_video', 'transcribe_video'})
        self.assertIn('summarize', tools['summarize_video'].description.lower())
        self.assertIn('path', tools['transcribe_video'].input_schema['properties'])


if __name__ == '__main__':
    unittest.main()
