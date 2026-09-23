"""Bounded parser for LVM's text format used by the synthetic recovery lab."""
import json
import re

TOKEN = re.compile(r'\s+|#[^\n]*|"(?:\\.|[^"\\])*"|[A-Za-z_][\w.+-]*|-?\d+|[{}=\[\],]')


def parse(text):
    if len(text) > 1024 * 1024:
        raise ValueError('metadata record too large')
    tokens = []
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if match is None:
            raise ValueError('invalid metadata token')
        token = match.group()
        if not token.isspace() and not token.startswith('#'):
            tokens.append(token)
        position = match.end()
    index = 0

    def take():
        nonlocal index
        if index >= len(tokens):
            raise ValueError('truncated metadata')
        token = tokens[index]
        index += 1
        return token

    def value():
        token = take()
        if token == '[':
            items = []
            if index < len(tokens) and tokens[index] == ']':
                take()
                return items
            while True:
                item = value()
                if isinstance(item, list):
                    raise ValueError('nested array')
                items.append(item)
                delimiter = take()
                if delimiter == ']':
                    return items
                if delimiter != ',':
                    raise ValueError('missing array delimiter')
        if token.startswith('"'):
            return json.loads(token)
        if re.fullmatch('-?[0-9]+', token):
            return int(token)
        raise ValueError('invalid value')

    def obj(depth=0):
        if depth > 8:
            raise ValueError('metadata nesting too deep')
        result = {}
        while index < len(tokens) and tokens[index] != '}':
            key, operator = take(), take()
            if not re.fullmatch(r'[A-Za-z_][\w.+-]*', key) or key in result:
                raise ValueError('invalid or duplicate key')
            if operator == '{':
                result[key] = obj(depth + 1)
                if take() != '}':
                    raise ValueError('missing closing brace')
            elif operator == '=':
                result[key] = value()
            else:
                raise ValueError('invalid operator')
        return result

    result = obj()
    if index != len(tokens):
        raise ValueError('trailing metadata')
    return result
