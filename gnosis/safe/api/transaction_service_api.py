import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin

import requests
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from hexbytes import HexBytes
from web3 import Web3

from gnosis.eth import EthereumNetwork
from gnosis.safe import SafeTx

from .base_api import SafeAPIException, SafeBaseAPI

logger = logging.getLogger(__name__)


class TransactionServiceApi(SafeBaseAPI):
    URL_BY_NETWORK = {
        EthereumNetwork.ARBITRUM: "https://safe-transaction.arbitrum.gnosis.io",
        EthereumNetwork.AURORA: "https://safe-transaction.aurora.gnosis.io",
        EthereumNetwork.AVALANCHE: "https://safe-transaction.avalanche.gnosis.io",
        EthereumNetwork.BINANCE: "https://safe-transaction.bsc.gnosis.io",
        EthereumNetwork.ENERGY_WEB_CHAIN: "https://safe-transaction.ewc.gnosis.io",
        EthereumNetwork.GOERLI: "https://safe-transaction.goerli.gnosis.io",
        EthereumNetwork.MAINNET: "https://safe-transaction.mainnet.gnosis.io",
        EthereumNetwork.MATIC: "https://safe-transaction.polygon.gnosis.io",
        EthereumNetwork.OPTIMISTIC: "https://safe-transaction.optimism.gnosis.io",
        EthereumNetwork.RINKEBY: "https://safe-transaction.rinkeby.gnosis.io",
        EthereumNetwork.VOLTA: "https://safe-transaction.volta.gnosis.io",
        EthereumNetwork.XDAI: "https://safe-transaction.xdai.gnosis.io",
        EthereumNetwork.ACA: "https://transaction.safe.acala.network",
    }

    @classmethod
    def create_delegate_message_hash(cls, delegate_address: str) -> str:
        totp = int(time.time()) // 3600
        hash_to_sign = Web3.keccak(text=delegate_address + str(totp))
        return hash_to_sign

    @classmethod
    def data_decoded_to_text(cls, data_decoded: Dict[str, Any]) -> Optional[str]:
        """
        Decoded data decoded to text
        :param data_decoded:
        :return:
        """
        if not data_decoded:
            return None

        method = data_decoded["method"]
        parameters = data_decoded.get("parameters", [])
        text = ""
        for (
            parameter
        ) in parameters:  # Multisend or executeTransaction from another Safe
            if "decodedValue" in parameter:
                text += (
                    method
                    + ":\n - "
                    + "\n - ".join(
                        [
                            cls.data_decoded_to_text(
                                decoded_value.get("decodedData", {})
                            )
                            for decoded_value in parameter.get("decodedValue", {})
                        ]
                    )
                    + "\n"
                )
        if text:
            return text.strip()
        else:
            return (
                method
                + ": "
                + ",".join([str(parameter["value"]) for parameter in parameters])
            )

    @classmethod
    def parse_signatures(cls, raw_tx: Dict[str, Any]) -> Optional[HexBytes]:
        if raw_tx["signatures"]:
            # Tx was executed and signatures field is populated
            return raw_tx["signatures"]
        elif raw_tx["confirmations"]:
            # Parse offchain transactions
            return b"".join(
                [
                    HexBytes(confirmation["signature"])
                    for confirmation in sorted(
                        raw_tx["confirmations"], key=lambda x: int(x["owner"], 16)
                    )
                    if confirmation["signatureType"] == "EOA"
                ]
            )

    def get_balances(self, safe_address: str) -> List[Dict[str, Any]]:
        response = self._get_request(f"/api/v1/safes/{safe_address}/balances/")
        if not response.ok:
            raise SafeAPIException(f"Cannot get balances: {response.content}")
        else:
            return response.json()

    def get_safe_transaction(
        self, safe_tx_hash: Union[bytes, HexStr]
    ) -> Tuple[SafeTx, Optional[HexBytes]]:
        """
        :param safe_tx_hash:
        :return: SafeTx and `tx-hash` if transaction was executed
        """
        safe_tx_hash = HexBytes(safe_tx_hash).hex()
        response = self._get_request(f"/api/v1/multisig-transactions/{safe_tx_hash}/")
        if not response.ok:
            raise SafeAPIException(
                f"Cannot get transaction with safe-tx-hash={safe_tx_hash}: {response.content}"
            )
        else:
            result = response.json()
            # TODO return tx-hash if executed
            signatures = self.parse_signatures(result)
            if not self.ethereum_client:
                logger.warning(
                    "EthereumClient should be defined to get a executable SafeTx"
                )
            safe_tx = SafeTx(
                self.ethereum_client,
                result["safe"],
                result["to"],
                int(result["value"]),
                HexBytes(result["data"]) if result["data"] else b"",
                int(result["operation"]),
                int(result["safeTxGas"]),
                int(result["baseGas"]),
                int(result["gasPrice"]),
                result["gasToken"],
                result["refundReceiver"],
                signatures=signatures if signatures else b"",
                safe_nonce=int(result["nonce"]),
                chain_id=self.network.value,
            )
            tx_hash = (
                HexBytes(result["transactionHash"])
                if result["transactionHash"]
                else None
            )
            if tx_hash:
                safe_tx.tx_hash = tx_hash
            return (safe_tx, tx_hash)

    def get_transactions(self, safe_address: str) -> List[Dict[str, Any]]:
        response = self._get_request(
            f"/api/v1/safes/{safe_address}/multisig-transactions/"
        )
        if not response.ok:
            raise SafeAPIException(f"Cannot get transactions: {response.content}")
        else:
            return response.json().get("results", [])

    def get_delegates(self, safe_address: str) -> List[Dict[str, Any]]:
        response = self._get_request(f"/api/v1/safes/{safe_address}/delegates/")
        if not response.ok:
            raise SafeAPIException(f"Cannot get delegates: {response.content}")
        else:
            return response.json().get("results", [])

    def post_signatures(self, safe_tx_hash: bytes, signatures: bytes) -> None:
        safe_tx_hash = HexBytes(safe_tx_hash).hex()
        response = self._post_request(
            f"/api/v1/multisig-transactions/{safe_tx_hash}/confirmations/",
            payload={"signature": HexBytes(signatures).hex()},
        )
        if not response.ok:
            raise SafeAPIException(
                f"Cannot post signatures for tx with safe-tx-hash={safe_tx_hash}: {response.content}"
            )

    def add_delegate(
        self,
        safe_address: str,
        delegate_address: str,
        label: str,
        signer_account: LocalAccount,
    ):
        hash_to_sign = self.create_delegate_message_hash(delegate_address)
        signature = signer_account.signHash(hash_to_sign)
        add_payload = {
            "safe": safe_address,
            "delegate": delegate_address,
            "signature": signature.signature.hex(),
            "label": label,
        }
        response = self._post_request(
            f"/api/v1/safes/{safe_address}/delegates/", add_payload
        )
        if not response.ok:
            raise SafeAPIException(f"Cannot add delegate: {response.content}")

    def remove_delegate(
        self, safe_address: str, delegate_address: str, signer_account: LocalAccount
    ):
        hash_to_sign = self.create_delegate_message_hash(delegate_address)
        signature = signer_account.signHash(hash_to_sign)
        remove_payload = {"signature": signature.signature.hex()}
        response = self._delete_request(
            f"/api/v1/safes/{safe_address}/delegates/{delegate_address}/",
            remove_payload,
        )
        if not response.ok:
            raise SafeAPIException(f"Cannot remove delegate: {response.content}")

    def post_transaction(self, safe_tx: SafeTx):
        url = urljoin(
            self.base_url,
            f"/api/v1/safes/{safe_tx.safe_address}/multisig-transactions/",
        )
        random_sender = "0x0000000000000000000000000000000000000002"
        sender = safe_tx.sorted_signers[0] if safe_tx.sorted_signers else random_sender
        data = {
            "to": safe_tx.to,
            "value": safe_tx.value,
            "data": safe_tx.data.hex() if safe_tx.data else None,
            "operation": safe_tx.operation,
            "gasToken": safe_tx.gas_token,
            "safeTxGas": safe_tx.safe_tx_gas,
            "baseGas": safe_tx.base_gas,
            "gasPrice": safe_tx.gas_price,
            "refundReceiver": safe_tx.refund_receiver,
            "nonce": safe_tx.safe_nonce,
            "contractTransactionHash": safe_tx.safe_tx_hash.hex(),
            "sender": sender,
            "signature": safe_tx.signatures.hex() if safe_tx.signatures else None,
            "origin": "Safe-CLI",
        }
        response = requests.post(url, json=data)
        if not response.ok:
            raise SafeAPIException(f"Error posting transaction: {response.content}")
