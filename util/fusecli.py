import argparse
from bitarray import bitarray
from contextlib import contextmanager

from . import database
from .fuseconv import read_jed, write_jed


db = database.load()


macrocell_options = [
    "pt_power",
    "pt1_mux",
    "pt2_mux",
    "pt3_mux",
    "global_reset",
    "pt4_mux",
    "pt4_func",
    "global_clock",
    "pt5_mux",
    "pt5_func",
    "xor_a_input",
    "d_mux",
    "dfast_mux",
    "storage",
    "fb_mux",
    "o_mux",
    "o_inv",
    "oe_mux",
    "slow_output",
    "open_collector",
    "pull_up",
    "schmitt_trigger",
    "bus_keeper",
    "low_power",
]


def extract_fuses(fuses, field, *, default=None):
    value = sum(fuses[fuse] << n_fuse for n_fuse, fuse in enumerate(field['fuses']))
    for key, key_value in field['values'].items():
        if value == key_value:
            return key
    if default is None:
        assert False, f"fuses {field['fuses']}: extracted {value}, known {field['values']}"
    return default


def replace_fuses(fuses, field, key):
    value = field['values'][key]
    for n_fuse, fuse in enumerate(field['fuses']):
        fuses[fuse] = (value >> n_fuse) & 1


def parse_filters(entities, history):
    filters = {}
    for entity in entities:
        entity_filter = filters
        levels = entity.split('.')
        new_history = []
        for n_level, level in enumerate(levels):
            if not level and len(history) > n_level:
                level = history[n_level]
            new_history.append(level)
            level = level.upper()
            if level not in entity_filter:
                entity_filter[level] = {}
            entity_filter = entity_filter[level]
        history.clear()
        history.extend(new_history)
    return filters


def match_filters(filters, patterns):
    if not filters:
        return True, {}
    for pattern in patterns:
        pattern = pattern.upper()
        if pattern in filters:
            return True, filters[pattern]
    else:
        return False, {}


def match_filters_last(filters, patterns):
    if not filters:
        return True
    for pattern in patterns:
        pattern = pattern.upper()
        if pattern in filters:
            if filters[pattern]:
                raise SystemExit(f"Cannot drill down into {pattern}")
            return True
    else:
        return False


class FuseTool:
    def __init__(self, device, fuses, *, verbose=False):
        self.device = device
        self.fuses = fuses
        self.verbose = verbose
        self._level = 0

    def print(self, *args, **kwargs):
        print(self._level * '  ', end='')
        print(*args, **kwargs)

    @contextmanager
    def hierarchy(self, name):
        self.print(f"{name}:")
        self._level += 1
        yield
        self._level -= 1

    def get_option(self, option_name, option):
        value = extract_fuses(self.fuses, option)
        if self.verbose:
            self.print("{:14} = {:5} {:8} [{}]".format(
                option_name,
                '(' + ''.join(str(self.fuses[n]&1) for n in option['fuses']) + ')',
                value,
                ','.join(map(str, option['fuses']))))
        else:
            self.print("{:14} = {}".format(option_name, value))

    def get_pterm(self, pterm_name, pterm, pterm_points):
        pterm_fuse_range = range(*pterm['fuse_range'])
        pterm_fuses = self.fuses[pterm_fuse_range.start:pterm_fuse_range.stop]
        if not pterm_fuses.count(0):
            value = 'VCC'
        elif not pterm_fuses.count(1):
            value = 'GND'
        else:
            expr = []
            for point_net, point_fuse in pterm_points.items():
                if pterm_fuses[point_fuse] == 0:
                    expr.append(point_net)
                    pterm_fuses[point_fuse] = 1
            for unknown_fuse in pterm_fuses.search(bitarray([0])):
                expr.append(f"_{unknown_fuse}")
            value = ' & '.join(expr)
        if self.verbose:
            self.print("{}: {:6} [{:5}..{:5}]".format(
                pterm_name, value, pterm_fuse_range.start, pterm_fuse_range.stop))
        else:
            self.print("{}: {}".format(pterm_name, value))

    def get_macrocell_config(self, macrocell, filters):
        with self.hierarchy('CFG'):
            for option_name in macrocell_options:
                if option_name not in macrocell:
                    continue
                if match_filters_last(filters, (option_name,)):
                    self.get_option(option_name, macrocell[option_name])

    def get_macrocell(self, macrocell_name, filters):
        for option_name in map(str.upper, macrocell_options):
            if option_name in filters:
                if 'CFG' not in filters:
                    filters['CFG'] = {}
                filters['CFG'][option_name] = filters.pop(option_name)

        with self.hierarchy(macrocell_name):
            macrocell = self.device['macrocells'][macrocell_name]

            pterm_points = self.device['blocks'][macrocell['block']]['pterm_points']
            for pterm_name, pterm in self.device['pterms'][macrocell_name].items():
                if match_filters_last(filters, ('PT', pterm_name)):
                    self.get_pterm(pterm_name, pterm, pterm_points)

            matched, subfilters = match_filters(filters, ('CFG',))
            if matched:
                self.get_macrocell_config(macrocell, subfilters)

    def get_goe_mux(self, goe_mux_name, filters):
        goe_mux = self.device['goe_muxes'][goe_mux_name]
        cross_mux_net = extract_fuses(self.fuses, goe_mux, default='(unknown)')
        if self.verbose:
            self.print("{}: ({}) {:6} [{:5}..{:5}]".format(
                goe_mux_name,
                ''.join(str(self.fuses[n]&1) for n in goe_mux['fuses']),
                cross_mux_net,
                min(goe_mux['fuses']), max(goe_mux['fuses'])))
        else:
            self.print("{}: {}".format(goe_mux_name, cross_mux_net))

    def get_device(self, filters):
        for macrocell_name in self.device['macrocells']:
            matched, subfilters = match_filters(filters, ('MC', macrocell_name))
            if matched:
                self.get_macrocell(macrocell_name, subfilters)

        for goe_mux_name in self.device['goe_muxes']:
            matched, subfilters = match_filters(filters, ('GOE', goe_mux_name))
            if matched:
                self.get_goe_mux(goe_mux_name, subfilters)

    def set_option(self, option_name, option, value):
        value = value.lower()
        self.print("{:14s} = {}".format(option_name, value))

        if value not in option['values']:
            raise SystemExit(f"Option {option_name} cannot be set to '{value}'; "
                             f"choose one of: {', '.join(option['values'])}")

        replace_fuses(self.fuses, option, value)
        return 1

    def set_pterm(self, pterm_name, pterm, pterm_points, value):
        value = value.upper()
        self.print(f"{pterm_name}: {value}")

        pterm_fuse_range = range(*pterm['fuse_range'])
        pterm_fuses = self.fuses[pterm_fuse_range.start:pterm_fuse_range.stop]
        if value == 'VCC':
            pterm_fuses.setall(1)
        elif value == 'GND':
            pterm_fuses.setall(0)
        else:
            def apply_net(net, value):
                if net in pterm_points:
                    point_fuse = pterm_points[net]
                elif net.startswith('_'):
                    point_fuse = int(net[1:])
                else:
                    raise SystemExit(f"Product term cannot contain net '{net}'; "
                                     f"choose one of: {', '.join(pterm_points)}")
                pterm_fuses[point_fuse] = value

            expr = value.split(',')
            if all(net.startswith('+') or net.startswith('-') for net in expr):
                for net in expr:
                    if net[0] == '+':
                        apply_net(net[1:], 0)
                    elif net[0] == '-':
                        apply_net(net[1:], 1)
                    else:
                        assert False
            elif any(net.startswith('+') or net.startswith('-') for net in expr):
                raise SystemExit(f"An expression should either modify existing product term "
                                 f"(using '+net,-net,...' syntax) or replace entire product "
                                 f"term (using 'net,net,...' syntax")
            else:
                pterm_fuses.setall(1)
                for net in expr:
                    apply_net(net, 0)

        self.fuses[pterm_fuse_range.start:pterm_fuse_range.stop] = pterm_fuses
        return 1

    def set_macrocell_config(self, macrocell, filters, value):
        changed = 0

        with self.hierarchy('CFG'):
            for option_name in macrocell_options:
                if option_name not in macrocell:
                    continue
                if match_filters_last(filters.get('CFG', filters), (option_name,)):
                    changed += self.set_option(option_name, macrocell[option_name], value)

        return changed

    def set_macrocell(self, macrocell_name, filters, value):
        changed = 0

        for option_name in map(str.upper, macrocell_options):
            if option_name in filters:
                if 'CFG' not in filters:
                    filters['CFG'] = {}
                filters['CFG'][option_name] = filters.pop(option_name)

        with self.hierarchy(macrocell_name):
            macrocell = self.device['macrocells'][macrocell_name]

            pterm_points = self.device['blocks'][macrocell['block']]['pterm_points']
            for pterm_name, pterm in self.device['pterms'][macrocell_name].items():
                if match_filters_last(filters, ('PT', pterm_name)):
                    changed += self.set_pterm(pterm_name, pterm, pterm_points, value)

            matched, subfilters = match_filters(filters, ('CFG',))
            if matched:
                changed += self.set_macrocell_config(macrocell, subfilters, value)

        return changed

    def set_goe_mux(self, goe_mux_name, filters, value):
        value = value.upper()
        self.print(f"{goe_mux_name}: {value}")

        goe_mux = self.device['goe_muxes'][goe_mux_name]
        if value not in goe_mux['values']:
            raise SystemExit(f"GOE mux {goe_mux_name} cannot select net '{value}'; "
                             f"choose one of: {', '.join(goe_mux['values'])}")

        replace_fuses(self.fuses, goe_mux, value)
        return 1

    def set_device(self, filters, value):
        changed = 0

        for macrocell_name in self.device['macrocells']:
            matched, subfilters = match_filters(filters, ('MC', macrocell_name))
            if matched:
                changed += self.set_macrocell(macrocell_name, subfilters, value)

        for goe_mux_name in self.device['goe_muxes']:
            matched, subfilters = match_filters(filters, ('GOE', goe_mux_name))
            if matched:
                changed += self.set_goe_mux(goe_mux_name, subfilters, value)

        return changed


def main():
    parser = argparse.ArgumentParser(description='Examine and modify fuses.')
    parser.add_argument(
        '-d', '--device', metavar='DEVICE', choices=db, default='ATF1502AS',
        help='device (one of: %(choices)s)')
    parser.add_argument(
        '-f', '--file', metavar='JED-FILE', type=argparse.FileType('r+'), required=True,
        help='operate on JESD3-C fuse file JED-FILE')
    parser.add_argument(
        '-v', '--verbose', default=False, action='store_true',
        help='show fuse numbers and values')
    subparsers = parser.add_subparsers(
        metavar='COMMAND', dest='command', required=True)

    get_parser = subparsers.add_parser('get', help='examine fuse states')
    get_parser.add_argument(
        'entities', metavar='ENTITY', type=str, nargs='*', default=[],
        help='examine fuses of ENTITY (e.g.: MC0, MC.PT1, MC1.pt3_mux)')

    set_parser = subparsers.add_parser('set', help='modify fuse states')
    set_parser.add_argument(
        'actions', metavar='ENTITY VALUE', type=str, nargs=argparse.REMAINDER,
        help='modify fuses of ENTITY to be VALUE (e.g.: MC0.PT1 +MC32_FLB)')

    args = parser.parse_args()

    device = db[args.device]

    orig_fuses, jed_comment = read_jed(args.file)
    fuses = bitarray(orig_fuses)

    history = []
    tool = FuseTool(device, fuses, verbose=args.verbose)

    if args.command == 'get':
        tool.get_device(parse_filters(args.entities, history))

    if args.command == 'set':
        if len(args.actions) % 2 != 0:
            raise SystemExit(f"Actions must be specified in pairs")

        changed = 0
        for entity, value in zip(args.actions[0::2], args.actions[1::2]):
            action_changed = tool.set_device(parse_filters((entity,), history), value)
            if action_changed == 0:
                raise SystemExit(f"Filter '{entity}' does not match anything")
            changed += action_changed
        changed_fuses = (orig_fuses ^ fuses).count(1)
        print(f"Changed {changed} fields, {changed_fuses} fuses.")

        jed_comment += f"Edited: set {' '.join(args.actions)}\n"

    if fuses != orig_fuses:
        args.file.seek(0)
        args.file.truncate()
        write_jed(args.file, fuses, comment=jed_comment)


if __name__ == '__main__':
    main()
