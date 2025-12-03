import unittest
import asyncio
import json
from datetime import datetime
from agents.base_agent import Message, BaseAgent

class MockAgent(BaseAgent):
    async def on_message(self, message):
        pass
    async def run(self):
        pass

class TestMessage(unittest.TestCase):
    def test_message_creation(self):
        data = {"token": "0x123", "amount": 100}
        msg = Message("TEST_TYPE", data, "sender_agent")
        
        self.assertEqual(msg.type, "TEST_TYPE")
        self.assertEqual(msg.data, data)
        self.assertEqual(msg.sender, "sender_agent")
        self.assertIsNotNone(msg.id)
        self.assertIsNotNone(msg.timestamp)

    def test_serialization(self):
        data = {"token": "0x123"}
        msg = Message("TEST", data)
        json_str = msg.to_json()
        
        msg2 = Message.from_json(json_str)
        self.assertEqual(msg.id, msg2.id)
        self.assertEqual(msg.type, msg2.type)
        self.assertEqual(msg.data, msg2.data)

class TestBaseAgent(unittest.IsolatedAsyncioTestCase):
    async def test_agent_initialization(self):
        agent = MockAgent("TestAgent")
        self.assertEqual(agent.name, "TestAgent")
        self.assertFalse(agent.running)
        
    async def test_memory_bus(self):
        agent1 = MockAgent("Agent1")
        agent2 = MockAgent("Agent2")
        
        received = []
        async def callback(msg):
            received.append(msg)
            
        # Manually subscribe for test (bypassing redis check for simplicity)
        from agents.base_agent import _memory_bus
        _memory_bus["test_channel"].append(callback)
        
        msg = Message("TEST", {})
        await agent1.publish("test_channel", msg)
        
        # Allow async loop to process
        await asyncio.sleep(0.1)
        
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].type, "TEST")

if __name__ == '__main__':
    unittest.main()
