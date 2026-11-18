"""RESP2 파서/인코더 단위 테스트."""

import unittest

from resp import (
    MAX_ARGS,
    MAX_HEADER_LEN,
    ProtocolError,
    bulk,
    encode_command,
    error,
    integer,
    parse_command,
    simple,
)

MAX_BULK = 1024


class ParseCommandTest(unittest.TestCase):
    def test_parses_single_command(self):
        data = b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n"
        self.assertEqual(parse_command(data, 0, MAX_BULK), ([b"GET", b"foo"], len(data)))

    def test_architecture_example(self):
        data = b"*2\r\n$3\r\nSET\r\n$12\r\nname:Kim Min\r\n"
        args, _ = parse_command(data, 0, MAX_BULK)
        self.assertEqual(args, [b"SET", b"name:Kim Min"])

    def test_every_incomplete_prefix_returns_none(self):
        data = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$5\r\nhello\r\n"
        for cut in range(len(data)):
            self.assertIsNone(parse_command(data[:cut], 0, MAX_BULK), f"cut={cut}")

    def test_pipelined_commands_are_parsed_one_by_one(self):
        first = b"*1\r\n$4\r\nPING\r\n"
        second = b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"
        args, pos = parse_command(first + second, 0, MAX_BULK)
        self.assertEqual((args, pos), ([b"PING"], len(first)))
        args, pos = parse_command(first + second, pos, MAX_BULK)
        self.assertEqual((args, pos), ([b"GET", b"k"], len(first) + len(second)))

    def test_value_is_binary_safe(self):
        value = b"a\r\nb\x00\xff"
        data = b"*2\r\n$3\r\nGET\r\n$%d\r\n%s\r\n" % (len(value), value)
        self.assertEqual(parse_command(data, 0, MAX_BULK)[0], [b"GET", value])

    def test_empty_bulk_string(self):
        self.assertEqual(parse_command(b"*1\r\n$0\r\n\r\n", 0, MAX_BULK)[0], [b""])

    def test_bulk_exactly_at_limit_is_accepted(self):
        data = b"*1\r\n$%d\r\n%s\r\n" % (MAX_BULK, b"x" * MAX_BULK)
        self.assertEqual(len(parse_command(data, 0, MAX_BULK)[0][0]), MAX_BULK)

    def test_bulk_over_limit_is_rejected_before_payload_arrives(self):
        with self.assertRaises(ProtocolError):
            parse_command(b"*1\r\n$%d\r\n" % (MAX_BULK + 1), 0, MAX_BULK)

    def test_inline_command_is_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_command(b"PING\r\n", 0, MAX_BULK)

    def test_non_numeric_lengths_are_rejected(self):
        for data in (b"*x\r\n", b"*1\r\n$+3\r\nabc\r\n", b"*1\r\n$ 3\r\nabc\r\n", b"*1\r\n$-1\r\n"):
            with self.assertRaises(ProtocolError, msg=data):
                parse_command(data, 0, MAX_BULK)

    def test_bulk_without_crlf_terminator_is_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_command(b"*1\r\n$3\r\nabcXX", 0, MAX_BULK)

    def test_empty_array_yields_no_arguments(self):
        self.assertEqual(parse_command(b"*0\r\n", 0, MAX_BULK), ([], 4))

    def test_too_many_arguments_are_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_command(b"*%d\r\n" % (MAX_ARGS + 1), 0, MAX_BULK)

    def test_endless_header_line_is_rejected(self):
        with self.assertRaises(ProtocolError):
            parse_command(b"*" + b"1" * (MAX_HEADER_LEN + 1), 0, MAX_BULK)


class EncodeTest(unittest.TestCase):
    def test_reply_encoders(self):
        self.assertEqual(simple("OK"), b"+OK\r\n")
        self.assertEqual(error("ERR bad"), b"-ERR bad\r\n")
        self.assertEqual(integer(-2), b":-2\r\n")
        self.assertEqual(bulk(b"a\r\nb"), b"$4\r\na\r\nb\r\n")
        self.assertEqual(bulk(None), b"$-1\r\n")

    def test_encode_command_round_trips_through_parser(self):
        data = encode_command("SET", b"k\x00", 10)
        self.assertEqual(parse_command(data, 0, MAX_BULK)[0], [b"SET", b"k\x00", b"10"])


if __name__ == "__main__":
    unittest.main()
