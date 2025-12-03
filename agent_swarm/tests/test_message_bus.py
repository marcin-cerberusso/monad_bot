#!/usr/bin/env python3
"""
🧪 BUS TESTS - Testy integracyjne MessageBus

Testy:
- pub/sub in-memory
- pub/sub z Redis (jeśli dostępny)
- consensus happy path
- consensus timeout
- allowed_senders blokuje/pozwala
- signal_* helpery
- walidacja payloadów
- metryki
"""

import asyncio
import pytest
import sys
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Dodaj parent do path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_swarm.message_bus import MessageBus, get_bus, shutdown_all
from agent_swarm.message_types import (
    Message, MessageType, Priority,
    WhaleAlertPayload, ConsensusRequestPayload, TradeAction,
    MessageBuilder
)
from agent_swarm.message_validator import validate_message, ValidationResult
from agent_swarm.bus_metrics import get_metrics, reset_metrics
from agent_swarm.bus_config import BusConfig, BusMode, get_config


class TestMessageBusInMemory:
    """Testy in-memory bus (bez Redis)"""
    
    @pytest.fixture
    def bus(self):
        """Fixture: bus bez Redis"""
        bus = MessageBus("test_agent")
        bus.connected = False  # Force in-memory mode
        return bus
        
    @pytest.mark.asyncio
    async def test_publish_to_local_queue(self, bus):
        """Test: publish dodaje do local queue"""
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="test",
            payload={"test": "data"}
        )
        
        # Wyłącz walidację dla testów z uproszczonymi payloadami
        await bus.publish(msg, "all", validate=False)
        
        # Powinno być w local queue
        assert not bus.local_queue.empty()
        queued = await bus.local_queue.get()
        assert queued.type == MessageType.WHALE_ALERT
        
    @pytest.mark.asyncio
    async def test_handler_called(self, bus):
        """Test: handler jest wywoływany"""
        received = []
        
        @bus.on(MessageType.WHALE_ALERT)
        async def handler(msg):
            received.append(msg)
            
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="other_agent",
            payload={}
        )
        
        await bus._handle_message(msg)
        
        assert len(received) == 1
        assert received[0].type == MessageType.WHALE_ALERT
        
    @pytest.mark.asyncio
    async def test_ignore_own_messages(self, bus):
        """Test: ignoruje własne wiadomości"""
        received = []
        
        @bus.on(MessageType.WHALE_ALERT)
        async def handler(msg):
            received.append(msg)
            
        # Wiadomość od samego siebie
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="test_agent",  # Same as bus.agent_name
            payload={}
        )
        
        await bus._handle_message(msg)
        
        assert len(received) == 0  # Nie wywołano handlera
        
    @pytest.mark.asyncio
    async def test_recipient_filtering(self, bus):
        """Test: filtrowanie po recipient"""
        received = []
        
        @bus.on(MessageType.WHALE_ALERT)
        async def handler(msg):
            received.append(msg)
            
        # Wiadomość do innego agenta
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="other",
            recipient="another_agent",  # Nie dla nas
            payload={}
        )
        
        await bus._handle_message(msg)
        assert len(received) == 0
        
        # Wiadomość do nas
        msg2 = Message(
            type=MessageType.WHALE_ALERT,
            sender="other",
            recipient="test_agent",  # Dla nas
            payload={}
        )
        
        await bus._handle_message(msg2)
        assert len(received) == 1
        

class TestAllowedSenders:
    """Testy ACL - allowed_senders"""
    
    @pytest.fixture
    def bus_with_acl(self):
        """Bus z ograniczeniami ACL"""
        bus = MessageBus("trader")
        bus.allowed_senders = {"analyst", "risk"}
        return bus
        
    @pytest.mark.asyncio
    async def test_allowed_sender_passes(self, bus_with_acl):
        """Test: dozwolony sender przechodzi"""
        received = []
        
        @bus_with_acl.on(MessageType.ANALYSIS_RESULT)
        async def handler(msg):
            received.append(msg)
            
        msg = Message(
            type=MessageType.ANALYSIS_RESULT,
            sender="analyst",  # Dozwolony
            payload={}
        )
        
        await bus_with_acl._handle_message(msg)
        assert len(received) == 1
        
    @pytest.mark.asyncio
    async def test_blocked_sender_rejected(self, bus_with_acl):
        """Test: niedozwolony sender jest blokowany"""
        received = []
        
        @bus_with_acl.on(MessageType.TRADE_SIGNAL)
        async def handler(msg):
            received.append(msg)
            
        msg = Message(
            type=MessageType.TRADE_SIGNAL,
            sender="unknown_agent",  # Niedozwolony
            payload={}
        )
        
        await bus_with_acl._handle_message(msg)
        assert len(received) == 0  # Zablokowane


class TestConsensus:
    """Testy consensus"""
    
    @pytest.mark.asyncio
    async def test_consensus_approved_with_votes(self):
        """Test: consensus zatwierdzony gdy wystarczająco głosów"""
        # Potrzebujemy dwóch busów
        requester = MessageBus("trader")
        voter = MessageBus("risk")
        
        requester.connected = False
        voter.connected = False
        
        # Symuluj głosowanie
        async def simulate_vote():
            await asyncio.sleep(0.1)
            # Dodaj głos bezpośrednio do pending
            for req_id, pending in requester.pending_consensus.items():
                pending.votes["risk"] = "approve"
                
        asyncio.create_task(simulate_vote())
        
        payload = ConsensusRequestPayload(
            action="buy",
            token_address="0x" + "1" * 40,
            token_name="TestToken",
            amount_mon=5.0,
            reason="test",
            timeout_seconds=1.0,
            min_approvals=1
        )
        
        result = await requester.request_consensus(payload)
        
        assert result == True
        
    @pytest.mark.asyncio
    async def test_consensus_timeout_rejects(self):
        """Test: consensus odrzucony po timeout bez głosów"""
        bus = MessageBus("trader")
        bus.connected = False
        
        payload = ConsensusRequestPayload(
            action="buy",
            token_address="0x" + "1" * 40,
            token_name="TestToken",
            amount_mon=5.0,
            reason="test",
            timeout_seconds=0.3,  # Bardzo krótki timeout
            min_approvals=2
        )
        
        result = await bus.request_consensus(payload)
        
        assert result == False  # Brak głosów = odrzucone


class TestSignalHelpers:
    """Testy signal_* helperów"""
    
    @pytest.fixture
    def bus(self):
        bus = MessageBus("scanner")
        bus.connected = False
        return bus
        
    @pytest.mark.asyncio
    async def test_signal_whale_alert(self, bus):
        """Test: signal_whale_alert tworzy poprawny payload"""
        await bus.signal_whale_alert(
            whale="0x" + "a" * 40,
            token="0x" + "b" * 40,
            action="buy",
            amount=1000.0,
            whale_name="BigWhale"
        )
        
        # Sprawdź że wiadomość trafiła do queue
        assert not bus.local_queue.empty()
        msg = await bus.local_queue.get()
        assert msg.type == MessageType.WHALE_ALERT
        
    @pytest.mark.asyncio
    async def test_signal_new_token(self, bus):
        """Test: signal_new_token tworzy poprawny payload"""
        await bus.signal_new_token(
            token="0x" + "c" * 40,
            name="TestToken",
            symbol="TEST",
            score=85
        )
        
        msg = await bus.local_queue.get()
        assert msg.type == MessageType.NEW_TOKEN
        
    @pytest.mark.asyncio
    async def test_signal_risk_alert(self, bus):
        """Test: signal_risk_alert tworzy poprawny payload"""
        await bus.signal_risk_alert(
            level="high",
            message="Whale dumping detected!",
            token="0x" + "d" * 40
        )
        
        msg = await bus.local_queue.get()
        assert msg.type == MessageType.RISK_ALERT


class TestMessageValidation:
    """Testy walidacji wiadomości"""
    
    def test_valid_message_passes(self):
        """Test: poprawna wiadomość przechodzi"""
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="scanner",
            payload={
                "whale_address": "0x" + "a" * 40,
                "token_address": "0x" + "b" * 40,
                "action": "buy",
                "amount_mon": 1000.0
            }
        )
        
        result = validate_message(msg)
        assert result.valid
        assert len(result.errors) == 0
        
    def test_missing_sender_fails(self):
        """Test: brak sender = błąd"""
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="",
            payload={"test": "data"}
        )
        
        result = validate_message(msg)
        assert not result.valid
        assert any("sender" in str(e).lower() for e in result.errors)
        
    def test_missing_required_field_fails(self):
        """Test: brak wymaganego pola = błąd"""
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="scanner",
            payload={
                # Brak whale_address, token_address, etc
            }
        )
        
        result = validate_message(msg)
        assert not result.valid
        
    def test_negative_amount_fails(self):
        """Test: ujemna kwota = błąd"""
        msg = Message(
            type=MessageType.WHALE_ALERT,
            sender="scanner",
            payload={
                "whale_address": "0x" + "a" * 40,
                "token_address": "0x" + "b" * 40,
                "action": "buy",
                "amount_mon": -100.0  # Ujemne!
            }
        )
        
        result = validate_message(msg)
        assert not result.valid


class TestMetrics:
    """Testy metryk"""
    
    def setup_method(self):
        """Reset metryk przed każdym testem"""
        reset_metrics()
        
    def test_record_send(self):
        """Test: zapisywanie wysłania"""
        metrics = get_metrics()
        
        metrics.record_send(
            channel="all",
            msg_type=MessageType.WHALE_ALERT,
            message_id="msg1",
            size_bytes=100
        )
        
        assert metrics.channels["all"].messages_sent == 1
        assert metrics.channels["all"].bytes_sent == 100
        assert metrics.types[MessageType.WHALE_ALERT].count == 1
        
    def test_record_receive_calculates_latency(self):
        """Test: latency jest liczone"""
        metrics = get_metrics()
        
        # Najpierw send
        metrics.record_send("all", MessageType.WHALE_ALERT, "msg1", 100)
        
        # Potem receive (symuluj opóźnienie)
        import time
        time.sleep(0.01)  # 10ms
        metrics.record_receive("all", MessageType.WHALE_ALERT, "msg1", 100)
        
        # Latency powinna być > 0
        assert metrics.types[MessageType.WHALE_ALERT].avg_latency_ms > 0
        
    def test_consensus_metrics(self):
        """Test: metryki consensus"""
        metrics = get_metrics()
        
        metrics.record_consensus_request()
        metrics.record_consensus_result(approved=True, votes=3)
        
        assert metrics.consensus.requests == 1
        assert metrics.consensus.approved == 1
        assert metrics.consensus.total_votes == 3
        
    def test_prometheus_export(self):
        """Test: eksport Prometheus"""
        metrics = get_metrics()
        metrics.record_send("all", MessageType.WHALE_ALERT, "msg1", 100)
        
        prom = metrics.to_prometheus()
        
        assert "monad_bus_messages_total" in prom
        assert "monad_bus_channel_sent" in prom


class TestMessageBuilder:
    """Testy MessageBuilder"""
    
    def test_whale_alert_builder(self):
        """Test: builder whale_alert"""
        payload = WhaleAlertPayload(
            whale_address="0x" + "w" * 40,
            whale_name="BigWhale",
            token_address="0x" + "a" * 40,
            token_name="TestToken",
            action=TradeAction.BUY,
            amount_mon=5000.0
        )
        
        msg = MessageBuilder.whale_alert("scanner", payload)
        
        assert msg.type == MessageType.WHALE_ALERT
        assert msg.sender == "scanner"
        assert msg.priority == Priority.HIGH
        
    def test_consensus_request_builder(self):
        """Test: builder consensus_request"""
        payload = ConsensusRequestPayload(
            action="buy",
            token_address="0x" + "b" * 40,
            token_name="TestToken",
            amount_mon=10.0,
            reason="Test",
            timeout_seconds=5.0,
            min_approvals=2
        )
        
        msg = MessageBuilder.consensus_request("trader", payload)
        
        assert msg.type == MessageType.CONSENSUS_REQUEST
        assert msg.sender == "trader"


class TestRateLimiting:
    """Testy rate limiting"""
    
    @pytest.fixture
    def bus(self):
        """Fixture: bus z rate limiting"""
        bus = MessageBus("test_agent")
        bus.connected = False
        return bus
        
    def test_rate_limit_check_allows_normal(self, bus):
        """Test: normalne wiadomości przechodzą"""
        from agent_swarm.bus_config import reset_config
        reset_config()  # Reset do defaults
        
        # First request should pass
        result = bus._check_rate_limit(Priority.NORMAL)
        assert result is True
        
    def test_rate_limit_critical_always_passes(self, bus):
        """Test: CRITICAL pomija rate limit"""
        from agent_swarm.bus_config import reset_config
        reset_config()
        
        # Zużyj wszystkie tokeny
        bus._rate_limit_tokens = 0
        
        # CRITICAL powinien przejść
        result = bus._check_rate_limit(Priority.CRITICAL)
        assert result is True
        
    def test_rate_limit_exhausted_blocks(self, bus):
        """Test: brak tokenów blokuje"""
        from agent_swarm.bus_config import reset_config
        reset_config()
        
        # Zużyj tokeny
        bus._rate_limit_tokens = 0
        
        # NORMAL powinien być zablokowany
        result = bus._check_rate_limit(Priority.NORMAL)
        assert result is False
        assert bus.stats["rate_limited"] == 1


class TestDynamicChannels:
    """Testy dynamic channels"""
    
    @pytest.fixture
    def bus(self):
        """Fixture: bus bez Redis"""
        bus = MessageBus("test_agent")
        bus.connected = False
        return bus
        
    @pytest.mark.asyncio
    async def test_register_channel(self, bus):
        """Test: rejestracja kanału"""
        channel = await bus.register_channel("my_custom_channel")
        
        assert "my_custom_channel" in channel
        assert channel in bus._registered_channels
        
    @pytest.mark.asyncio
    async def test_unregister_channel(self, bus):
        """Test: wyrejestrowanie kanału"""
        await bus.register_channel("temp_channel")
        await bus.unregister_channel("temp_channel")
        
        full_channel = "monad_bot:temp_channel"
        assert full_channel not in bus._registered_channels
        
    @pytest.mark.asyncio
    async def test_get_registered_channels(self, bus):
        """Test: pobierz listę kanałów"""
        await bus.register_channel("channel1")
        await bus.register_channel("channel2")
        
        channels = await bus.get_registered_channels()
        
        assert len(channels) >= 2
        assert any("channel1" in c for c in channels)
        assert any("channel2" in c for c in channels)


class TestEnhancedConsensus:
    """Testy enhanced consensus z wagami i veto"""
    
    def test_calculate_weighted_votes(self):
        """Test: obliczanie ważonych głosów"""
        bus = MessageBus("test")
        bus.connected = False
        
        from agent_swarm.bus_config import get_config, reset_config
        reset_config()
        config = get_config()
        
        votes = {
            "risk": "approve",      # weight 2.0
            "analyst": "approve",   # weight 1.5
            "scanner": "reject",    # weight 1.0
        }
        
        approvals = bus._calculate_weighted_votes(votes, "approve", config)
        rejections = bus._calculate_weighted_votes(votes, "reject", config)
        
        assert approvals == 3.5  # 2.0 + 1.5
        assert rejections == 1.0
        
    def test_veto_check(self):
        """Test: sprawdzenie veto"""
        bus = MessageBus("test")
        bus.connected = False
        
        from agent_swarm.bus_config import get_config, reset_config
        reset_config()
        config = get_config()
        
        # Bez veto
        votes_normal = {"analyst": "reject", "scanner": "approve"}
        assert bus._check_veto(votes_normal, config) is False
        
        # Z veto (risk jest w veto_agents)
        votes_veto = {"risk": "reject", "scanner": "approve"}
        assert bus._check_veto(votes_veto, config) is True
        
    def test_voter_weights_configured(self):
        """Test: wagi są skonfigurowane"""
        from agent_swarm.bus_config import get_config, reset_config
        reset_config()
        config = get_config()
        
        assert config.consensus.voter_weights["risk"] == 2.0
        assert config.consensus.voter_weights["analyst"] == 1.5
        assert config.consensus.voter_weights["orchestrator"] == 3.0
        
    def test_veto_agents_configured(self):
        """Test: veto agents są skonfigurowane"""
        from agent_swarm.bus_config import get_config, reset_config
        reset_config()
        config = get_config()
        
        assert "risk" in config.consensus.veto_agents


# === RUN TESTS ===

def run_tests():
    """Uruchom wszystkie testy"""
    print("=" * 60)
    print("🧪 RUNNING MESSAGE BUS TESTS")
    print("=" * 60)
    
    # Użyj pytest
    pytest_args = [
        __file__,
        "-v",
        "--tb=short",
        "-x",  # Stop on first failure
    ]
    
    exit_code = pytest.main(pytest_args)
    
    if exit_code == 0:
        print("\n✅ ALL TESTS PASSED!")
    else:
        print(f"\n❌ TESTS FAILED (exit code: {exit_code})")
        
    return exit_code


if __name__ == "__main__":
    run_tests()
