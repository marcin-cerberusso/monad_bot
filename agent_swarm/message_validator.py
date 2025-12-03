#!/usr/bin/env python3
"""
✅ MESSAGE VALIDATOR - Walidacja payloadów wiadomości

Sprawdza:
- Schemat payloadu (wymagane pola)
- Typy danych
- Limity wielkości
- Bezpieczeństwo (sanityzacja)
"""

import json
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional, Set, Type, get_type_hints
from datetime import datetime

from .message_types import (
    Message, MessageType, Priority,
    WhaleAlertPayload, NewTokenPayload, AnalysisRequestPayload,
    AnalysisResultPayload, TradeSignalPayload, TradeExecutedPayload,
    RiskAlertPayload, ConsensusRequestPayload, ConsensusVotePayload,
    ConsensusResultPayload, AgentHeartbeatPayload, PriceUpdatePayload
)


@dataclass
class ValidationError:
    """Błąd walidacji"""
    field: str
    message: str
    value: Any = None
    
    def __str__(self):
        return f"{self.field}: {self.message}"


@dataclass
class ValidationResult:
    """Wynik walidacji"""
    valid: bool
    errors: List[ValidationError]
    warnings: List[str]
    
    def __bool__(self):
        return self.valid
        
    def __str__(self):
        if self.valid:
            return "✅ Valid"
        return f"❌ Invalid: {', '.join(str(e) for e in self.errors)}"


# Limity
MAX_MESSAGE_SIZE_BYTES = 64 * 1024  # 64KB
MAX_STRING_LENGTH = 1024
MAX_REASON_LENGTH = 2048
MAX_ADDRESS_LENGTH = 128
MIN_ADDRESS_LENGTH = 10


# Mapping typ wiadomości -> klasa payloadu
PAYLOAD_TYPES: Dict[MessageType, Type] = {
    MessageType.WHALE_ALERT: WhaleAlertPayload,
    MessageType.NEW_TOKEN: NewTokenPayload,
    MessageType.ANALYSIS_REQUEST: AnalysisRequestPayload,
    MessageType.ANALYSIS_RESULT: AnalysisResultPayload,
    MessageType.TRADE_SIGNAL: TradeSignalPayload,
    MessageType.TRADE_EXECUTED: TradeExecutedPayload,
    MessageType.RISK_ALERT: RiskAlertPayload,
    MessageType.CONSENSUS_REQUEST: ConsensusRequestPayload,
    MessageType.CONSENSUS_VOTE: ConsensusVotePayload,
    MessageType.CONSENSUS_RESULT: ConsensusResultPayload,
    MessageType.AGENT_HEARTBEAT: AgentHeartbeatPayload,
    MessageType.PRICE_UPDATE: PriceUpdatePayload,
}


# Wymagane pola per typ
REQUIRED_FIELDS: Dict[MessageType, Set[str]] = {
    MessageType.WHALE_ALERT: {"whale_address", "token_address", "action", "amount_mon"},
    MessageType.NEW_TOKEN: {"token_address", "token_name", "token_symbol"},
    MessageType.ANALYSIS_REQUEST: {"token_address", "analysis_type"},  # Fixed: was request_type
    MessageType.ANALYSIS_RESULT: {"token_address", "recommendation"},
    MessageType.TRADE_SIGNAL: {"action", "token_address"},
    MessageType.TRADE_EXECUTED: {"token_address", "action", "success"},
    MessageType.RISK_ALERT: {"level", "message"},
    MessageType.CONSENSUS_REQUEST: {"action", "token_address"},
    MessageType.CONSENSUS_VOTE: {"request_id", "vote"},
    MessageType.CONSENSUS_RESULT: {"request_id", "approved"},
    MessageType.AGENT_HEARTBEAT: {"agent_name", "status"},
    MessageType.PRICE_UPDATE: {"token_address", "price_usd"},
}


class MessageValidator:
    """
    Walidator wiadomości
    """
    
    def __init__(self, strict: bool = True):
        """
        Args:
            strict: Jeśli True, odrzuca wiadomości z błędami.
                   Jeśli False, tylko loguje ostrzeżenia.
        """
        self.strict = strict
        
    def validate(self, message: Message) -> ValidationResult:
        """Waliduj wiadomość"""
        errors: List[ValidationError] = []
        warnings: List[str] = []
        
        # 1. Sprawdź rozmiar
        try:
            json_str = message.to_json()
            size = len(json_str.encode('utf-8'))
            if size > MAX_MESSAGE_SIZE_BYTES:
                errors.append(ValidationError(
                    "size", 
                    f"Message too large: {size} bytes > {MAX_MESSAGE_SIZE_BYTES}"
                ))
        except Exception as e:
            errors.append(ValidationError("json", f"Cannot serialize: {e}"))
            
        # 2. Sprawdź podstawowe pola
        if not message.sender:
            errors.append(ValidationError("sender", "Sender is required"))
        elif len(message.sender) > MAX_STRING_LENGTH:
            errors.append(ValidationError("sender", f"Too long: {len(message.sender)}"))
            
        if not message.type:
            errors.append(ValidationError("type", "Type is required"))
            
        # 3. Sprawdź payload
        payload_errors, payload_warnings = self._validate_payload(
            message.type, message.payload
        )
        errors.extend(payload_errors)
        warnings.extend(payload_warnings)
        
        # 4. Sprawdź recipient
        if message.recipient and len(message.recipient) > MAX_STRING_LENGTH:
            warnings.append(f"Recipient very long: {len(message.recipient)}")
            
        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )
        
    def _validate_payload(
        self, 
        msg_type: MessageType, 
        payload: Any
    ) -> tuple[List[ValidationError], List[str]]:
        """Waliduj payload"""
        errors: List[ValidationError] = []
        warnings: List[str] = []
        
        if payload is None:
            errors.append(ValidationError("payload", "Payload is required"))
            return errors, warnings
            
        # Konwertuj do dict jeśli to dataclass
        if hasattr(payload, 'to_dict'):
            payload_dict = payload.to_dict()
        elif hasattr(payload, '__dict__'):
            payload_dict = vars(payload)
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            errors.append(ValidationError("payload", f"Invalid type: {type(payload)}"))
            return errors, warnings
            
        # Sprawdź wymagane pola
        required = REQUIRED_FIELDS.get(msg_type, set())
        for field in required:
            if field not in payload_dict or payload_dict[field] is None:
                errors.append(ValidationError(field, "Required field missing"))
            elif payload_dict[field] == "":
                warnings.append(f"Field '{field}' is empty string")
                
        # Walidacja specyficznych pól
        errors.extend(self._validate_addresses(payload_dict))
        errors.extend(self._validate_amounts(payload_dict))
        errors.extend(self._validate_strings(payload_dict))
        
        return errors, warnings
        
    def _validate_addresses(self, payload: dict) -> List[ValidationError]:
        """Waliduj adresy (token, whale, etc)"""
        errors = []
        address_fields = ["token_address", "whale_address", "creator"]
        
        for field in address_fields:
            if field in payload and payload[field]:
                addr = payload[field]
                if not isinstance(addr, str):
                    errors.append(ValidationError(field, f"Must be string, got {type(addr)}"))
                elif len(addr) < MIN_ADDRESS_LENGTH:
                    errors.append(ValidationError(field, f"Too short: {len(addr)} < {MIN_ADDRESS_LENGTH}"))
                elif len(addr) > MAX_ADDRESS_LENGTH:
                    errors.append(ValidationError(field, f"Too long: {len(addr)} > {MAX_ADDRESS_LENGTH}"))
                # Podstawowa walidacja hex (opcjonalnie)
                # if not addr.startswith("0x"):
                #     errors.append(ValidationError(field, "Must start with 0x"))
                    
        return errors
        
    def _validate_amounts(self, payload: dict) -> List[ValidationError]:
        """Waliduj kwoty"""
        errors = []
        amount_fields = ["amount_mon", "price_usd", "price_mon", "confidence"]
        
        for field in amount_fields:
            if field in payload and payload[field] is not None:
                val = payload[field]
                if not isinstance(val, (int, float)):
                    errors.append(ValidationError(field, f"Must be number, got {type(val)}"))
                elif val < 0:
                    errors.append(ValidationError(field, f"Cannot be negative: {val}"))
                elif field == "confidence" and val > 100:
                    errors.append(ValidationError(field, f"Confidence cannot exceed 100: {val}"))
                    
        return errors
        
    def _validate_strings(self, payload: dict) -> List[ValidationError]:
        """Waliduj stringi"""
        errors = []
        string_limits = {
            "reason": MAX_REASON_LENGTH,
            "message": MAX_REASON_LENGTH,
            "token_name": MAX_STRING_LENGTH,
            "token_symbol": 32,
            "whale_name": MAX_STRING_LENGTH,
            "current_task": MAX_STRING_LENGTH,
        }
        
        for field, max_len in string_limits.items():
            if field in payload and payload[field]:
                val = payload[field]
                if not isinstance(val, str):
                    errors.append(ValidationError(field, f"Must be string, got {type(val)}"))
                elif len(val) > max_len:
                    errors.append(ValidationError(field, f"Too long: {len(val)} > {max_len}"))
                    
        return errors
        
    def validate_or_raise(self, message: Message):
        """Waliduj i rzuć wyjątek jeśli niepoprawne"""
        result = self.validate(message)
        if not result.valid:
            raise ValueError(f"Invalid message: {result}")
        return result


# Singleton
_validator: Optional[MessageValidator] = None

def get_validator(strict: bool = True) -> MessageValidator:
    """Pobierz singleton walidatora"""
    global _validator
    if _validator is None:
        _validator = MessageValidator(strict=strict)
    return _validator


def validate_message(message: Message) -> ValidationResult:
    """Szybka walidacja"""
    return get_validator().validate(message)


def validate_payload(msg_type: MessageType, payload: dict) -> ValidationResult:
    """Waliduj tylko payload"""
    dummy = Message(type=msg_type, sender="validator", payload=payload)
    return get_validator().validate(dummy)
