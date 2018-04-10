#!/usr/bin/env python3
"""
  Lightning network interface for Electrum
  Derived from https://gist.github.com/AdamISZ/046d05c156aaeb56cc897f85eecb3eb8
"""

import asyncio
import sys
import binascii
import hashlib
import hmac
import cryptography.hazmat.primitives.ciphers.aead as AEAD

from electrum.bitcoin import public_key_from_private_key, ser_to_point, point_to_ser, string_to_number
from electrum.bitcoin import int_to_hex, bfh, rev_hex

tcp_socket_timeout = 10
server_response_timeout = 60

def decode(string):
    """Return the integer value of the
    bytestring b
    """
    if isinstance(string, str):
        string = bytes(bytearray.fromhex(string))
    result = 0
    while len(string) > 0:
        result *= 256
        result += string[0]
        string = string[1:]
    return result


def encode(n, s):
    """Return a bytestring version of the integer
    value n, with a string length of s
    """
    return bfh(rev_hex(int_to_hex(n, s)))


def H256(data):
    return hashlib.sha256(data).digest()

class HandshakeState(object):
    prologue = b"lightning"
    protocol_name = b"Noise_XK_secp256k1_ChaChaPoly_SHA256"
    handshake_version = b"\x00"
    def __init__(self, responder_pub):
        self.responder_pub = responder_pub
        self.h = H256(self.protocol_name)
        self.ck = self.h
        self.update(self.prologue)
        self.update(self.responder_pub)

    def update(self, data):
        self.h = H256(self.h + data)
        return self.h
        
def get_nonce_bytes(n):
    """BOLT 8 requires the nonce to be 12 bytes, 4 bytes leading
    zeroes and 8 bytes little endian encoded 64 bit integer.
    """
    nb = b"\x00"*4
    #Encode the integer as an 8 byte byte-string
    nb2 = encode(n, 8)
    nb2 = bytearray(nb2)
    #Little-endian is required here
    nb2.reverse()
    return nb + nb2

def aead_encrypt(k, nonce, associated_data, data):
    nonce_bytes = get_nonce_bytes(nonce)
    a = AEAD.ChaCha20Poly1305(k)
    return a.encrypt(nonce_bytes, data, associated_data)

def aead_decrypt(k, nonce, associated_data, data):
    nonce_bytes = get_nonce_bytes(nonce)
    a = AEAD.ChaCha20Poly1305(k)
    #raises InvalidTag exception if it's not valid
    return a.decrypt(nonce_bytes, data, associated_data)

def get_bolt8_hkdf(salt, ikm):
    """RFC5869 HKDF instantiated in the specific form
    used in Lightning BOLT 8:
    Extract and expand to 64 bytes using HMAC-SHA256,
    with info field set to a zero length string as per BOLT8
    Return as two 32 byte fields.
    """
    #Extract
    prk = hmac.new(salt, msg=ikm, digestmod=hashlib.sha256).digest()
    assert len(prk) == 32
    #Expand
    info = b""
    T0 = b""
    T1 = hmac.new(prk, T0 + info + b"\x01", digestmod=hashlib.sha256).digest()
    T2 = hmac.new(prk, T1 + info + b"\x02", digestmod=hashlib.sha256).digest()
    assert len(T1 + T2) == 64
    return T1, T2

def get_ecdh(priv, pub):
    s = string_to_number(priv)
    pk = ser_to_point(pub)
    pt = point_to_ser(pk * s)
    return H256(pt)

def act1_initiator_message(hs, my_privkey):
    #Get a new ephemeral key
    epriv, epub = create_ephemeral_key(my_privkey)
    hs.update(epub)
    ss = get_ecdh(epriv, hs.responder_pub)
    ck2, temp_k1 = get_bolt8_hkdf(hs.ck, ss)
    hs.ck = ck2
    c = aead_encrypt(temp_k1, 0, hs.h, b"")
    #for next step if we do it
    hs.update(c)
    msg = hs.handshake_version + epub + c
    assert len(msg) == 50
    return msg

def privkey_to_pubkey(priv):
    pub = public_key_from_private_key(priv[:32], True)
    return bytes.fromhex(pub)
    
def create_ephemeral_key(privkey):
    pub = privkey_to_pubkey(privkey)
    return (privkey[:32], pub)

def process_message(message):
    print("Received %d bytes: "%len(message), binascii.hexlify(message))

def send_message(writer, msg, sk, sn):
    print("Sending %d bytes: "%len(msg), binascii.hexlify(msg))
    l = encode(len(msg), 2)
    lc = aead_encrypt(sk, sn, b'', l)
    c = aead_encrypt(sk, sn+1, b'', msg)
    assert len(lc) == 18
    assert len(c) == len(msg) + 16
    writer.write(lc+c)


async def read_message(reader, rk, rn):
    rspns = b''
    while True:
        rspns += await reader.read(2**10)
        print("buffer %d bytes:"%len(rspns), binascii.hexlify(rspns))
        lc = rspns[:18]
        l = aead_decrypt(rk, rn, b'', lc)
        length = decode(l)
        if len(rspns) < 18 + length + 16:
            continue
        c = rspns[18:18 + length + 16]
        msg = aead_decrypt(rk, rn+1, b'', c)
        return msg



async def main_loop(my_privkey, host, port, pubkey, loop):
    reader, writer = await asyncio.open_connection(host, port, loop=loop)

    hs = HandshakeState(pubkey)
    msg = act1_initiator_message(hs, my_privkey)

    # handshake act 1
    writer.write(msg)
    rspns = await reader.read(2**10)
    assert len(rspns) == 50
    hver, alice_epub, tag = rspns[0], rspns[1:34], rspns[34:]
    assert bytes([hver]) == hs.handshake_version

    # handshake act 2
    hs.update(alice_epub)
    myepriv, myepub = create_ephemeral_key(my_privkey)
    ss = get_ecdh(myepriv, alice_epub)
    ck, temp_k2 = get_bolt8_hkdf(hs.ck, ss)
    hs.ck = ck
    p = aead_decrypt(temp_k2, 0, hs.h, tag)
    hs.update(tag)

    # handshake act 3
    my_pubkey = privkey_to_pubkey(my_privkey)
    c = aead_encrypt(temp_k2, 1, hs.h, my_pubkey)
    hs.update(c)
    ss = get_ecdh(my_privkey[:32], alice_epub)
    ck, temp_k3 = get_bolt8_hkdf(hs.ck, ss)
    hs.ck = ck
    t = aead_encrypt(temp_k3, 0, hs.h, b'')
    sk, rk = get_bolt8_hkdf(hs.ck, b'')
    msg = hs.handshake_version + c + t
    writer.write(msg)
    
    # init counters
    sn = 0
    rn = 0

    # read init
    msg = await read_message(reader, rk, rn)
    process_message(msg)
    rn += 2

    # send init
    init_msg = encode(16, 2) + encode(0, 2) +encode(0,2)
    send_message(writer, init_msg, sk, sn)
    sn += 2

    # send ping
    msg_type = 18
    num_pong_bytes = 4
    byteslen = 4
    ping_msg = encode(msg_type, 2) + encode(num_pong_bytes, 2) + encode(byteslen, 2) + b'\x00'*byteslen
    send_message(writer, ping_msg, sk, sn)
    sn += 2

    # read pong
    msg = await read_message(reader, rk, rn)
    process_message(msg)
    rn += 2
    
    # close socket
    writer.close()
    



node_list = [
    ('ecdsa.net', '9735', '038370f0e7a03eded3e1d41dc081084a87f0afa1c5b22090b4f3abb391eb15d8ff'),
    ('77.58.162.148', '9735', '022bb78ab9df617aeaaf37f6644609abb7295fad0c20327bccd41f8d69173ccb49')
    ]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        host, port, pubkey = sys.argv[1:4]
    else:
        host, port, pubkey = node_list[0]
    pubkey = binascii.unhexlify(pubkey)
    port = int(port)
    privkey = b"\x21"*32 + b"\x01"
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop(privkey, host, port, pubkey, loop))
    loop.close()