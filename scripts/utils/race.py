import sys

from jarowinkler import jarowinkler_similarity
from re import search, sub

from models.betfair import BSPMap
from models.race import RaceInfo, RunnerInfo

from utils.cleaning import clean_race, clean_string
from utils.going import get_surface
from utils.lps import get_lps_scale
from utils.region import get_region


regex_class = r'(\(|\s)(C|c)lass (\d|[A-Ha-h])(\)|\s)'
regex_group = r'(\(|\s)((G|g)rade|(G|g)roup) (\d|[A-Ca-c]|I*)(\)|\s)'

RACE_TYPE_CODES: dict[str, str] = {
    'F': 'Flat',
    'X': 'Flat',
    'H': 'Hurdle',
    'Y': 'Hurdle',
    'C': 'Chase',
    'U': 'Chase',
    'Z': 'Chase',
    'P': 'Chase',
    'B': 'NH Flat',
}


class VoidRaceError(Exception):
    pass


class Race:
    def __init__(
        self,
        data: dict | None,
        fields: list[str],
        bsp_map: BSPMap | None = None,
    ):
        if not data:
            raise VoidRaceError('No race result data')

        self.data: dict = data
        self.race_info: RaceInfo = RaceInfo()
        self.runner_info: RunnerInfo = RunnerInfo()

        self.build_race_info()
        self.build_runners()
        self.clean_non_completions()

        if bsp_map:
            self.join_betfair_data(bsp_map)

        self.csv_data: list[str] = self.create_csv_data(fields)

    def build_race_info(self) -> None:
        header = self.data['header']
        details = self.data['details']
        info = self.race_info

        info.date = self.data['raceDatetime'][:10]
        info.course_id = str(self.data['courseUid'])
        info.course = self.data['courseName']
        info.region = get_region(info.course_id)
        info.course_detail = ''

        info.race_id = str(self.data['raceId'])
        info.off = header.get('raceTime') or ''
        info.race_name = header.get('raceTitle') or ''

        info.going = header.get('going') or ''
        info.surface = get_surface(info.going)

        info.race_class = self.get_race_class(header.get('raceClass') or '')
        info.pattern = self.get_race_pattern()

        info.race_name = clean_race(info.race_name)

        info.age_band, info.rating_band = self.parse_race_bands(
            header.get('ratingBandAndAgesAllowed') or ''
        )
        info.sex_rest = self.sex_restricted()

        (
            info.dist,
            info.dist_y,
            info.dist_f,
            info.dist_m,
        ) = self.get_race_distances(header)

        info.race_type = self.get_race_type(header.get('raceTypeCode') or '')
        info.ran = str(details.get('numberOfRunners') or len(self.data['runners']))

    def build_runners(self) -> None:
        prizes = {
            prize['position']: prize['formatted']
            for prize in (self.data['header'].get('prizes') or [])
        }

        btn: list[str] = []
        ovr_btn: list[str] = []

        for runner in self.data['runners']:
            pedigree = runner.get('pedigree') or {}
            ri = self.runner_info

            pos = self.get_position(runner)

            ri.num.append(str(runner.get('saddleClothNo') or ''))
            ri.pos.append(pos)
            ri.draw.append((runner.get('drawLabel') or '').strip().strip('()'))
            ri.horse_id.append(str(runner.get('horseUid') or ''))
            ri.horse.append(self.name_with_region(runner.get('horseName'), runner.get('horseSuffix')))
            ri.age.append(str(runner.get('age') or ''))
            ri.sex.append(self.get_sex(pedigree))
            ri.hg.append(self.get_headgear(runner))
            ri.wgt.append(self.get_weight(runner))
            ri.lbs.append(self.get_lbs(runner))
            ri.sp.append((runner.get('odds') or '').replace('No Odds', '').strip())
            ri.jockey_id.append(self.id_from_url(runner.get('jockeyUrl')))
            ri.jockey.append(clean_string(runner.get('jockeyName') or ''))
            ri.trainer_id.append(self.id_from_url(runner.get('trainerUrl')))
            ri.trainer.append(clean_string(runner.get('trainerName') or ''))
            ri.owner_id.append(self.id_from_url(runner.get('ownerUrl')))
            ri.owner.append(clean_string(runner.get('ownerName') or ''))
            ri.prize.append(self.get_prize(pos, prizes))
            ri.ofr.append(self.get_rating(runner.get('officialRating')))
            ri.rpr.append(self.get_rating(runner.get('rpRating')))
            ri.ts.append(self.get_rating(runner.get('topspeed')))
            ri.sire_id.append(self.id_from_url(pedigree.get('sireUrl')))
            ri.sire.append(self.name_with_region(pedigree.get('sireName'), pedigree.get('sireSuffix')))
            ri.dam_id.append(self.id_from_url(pedigree.get('damUrl')))
            ri.dam.append(self.name_with_region(pedigree.get('damName'), pedigree.get('damSuffix')))
            ri.damsire_id.append(self.id_from_url(pedigree.get('damSireUrl')))
            ri.damsire.append(clean_string(pedigree.get('damSireName') or ''))
            ri.silk_url.append(runner.get('silkUrl') or '')
            ri.comment.append(self.get_comment(runner))

            runner_btn, runner_ovr_btn = self.get_distances(runner, pos)
            btn.append(runner_btn)
            ovr_btn.append(runner_ovr_btn)

        self.runner_info.dec = self.get_decimal_odds()
        self.runner_info.btn = btn
        self.runner_info.ovr_btn = ovr_btn
        self.runner_info.time = self.get_finishing_times()
        self.runner_info.secs = self.time_to_seconds(self.runner_info.time)

    def calculate_times(
        self, win_time: float, dist_btn: list[str], going: str, race_type: str
    ) -> list[str]:
        times: list[str] = []

        lps_scale = get_lps_scale(race_type, going)

        for dist in dist_btn:
            try:
                time = win_time + (float(dist) / lps_scale)
                minutes = int(time // 60)
                seconds = time % 60
                times.append(f'{minutes}:{seconds:05.2f}')
            except ValueError:
                times.append('')

        return times

    def clean_non_completions(self):
        for i, pos in enumerate(self.runner_info.pos):
            if not pos.isnumeric() and pos != 'DSQ':
                self.runner_info.time[i] = '-'
                self.runner_info.secs[i] = '-'
                self.runner_info.ovr_btn[i] = '-'
                self.runner_info.btn[i] = '-'

    def create_csv_data(self, fields: list[str]) -> list[str]:
        field_mapping = {'type': 'race_type', 'class': 'race_class', 'or': 'ofr'}

        race_values: list[str] = []
        runner_values: list[list[str]] = []

        for field in fields:
            actual_field = field_mapping.get(field, field)

            if hasattr(self.race_info, actual_field):
                race_values.append(str(getattr(self.race_info, actual_field)))
            elif hasattr(self.runner_info, actual_field):
                runner_values.append([str(v) for v in getattr(self.runner_info, actual_field)])

        race_prefix = ','.join(race_values)
        rows: list[str] = []
        for row in zip(*runner_values, strict=False):
            rows.append(race_prefix + ',' + ','.join(row) if race_prefix else ','.join(row))
        return rows

    def get_comment(self, runner: dict) -> str:
        comment = (runner.get('comment') or {}).get('comment') or ''
        return (
            comment.strip()
            .replace('  ', '')
            .replace(',', ' -')
            .replace('\n', ' ')
            .replace('\r', '')
        )

    def get_decimal_odds(self) -> list[str]:
        odds = [sub('(F|J|C)', '', sp) for sp in self.runner_info.sp]
        return fraction_to_decimal(odds)

    def get_distances(self, runner: dict, pos: str) -> tuple[str, str]:
        btn = runner.get('beatenDistance')
        ovr_btn = runner.get('beatenDistanceToWinner')

        if btn is None:
            btn = '0' if pos.isnumeric() else ''
        else:
            btn = distance_to_decimal(btn)

        if ovr_btn is None:
            ovr_btn = btn
        else:
            ovr_btn = distance_to_decimal(ovr_btn)

        return btn, ovr_btn

    def get_finishing_times(self) -> list[str]:
        # adjust overall distance beaten when margins under a quarter length not accounted for
        # for instance when 2 horses finish with a head between them 4 lengths behind winner
        # both horses are recorded as being beaten 4 lengths overall by RP

        btn_adj: list[str] = []

        for btn, ovr_btn in zip(self.runner_info.btn, self.runner_info.ovr_btn):
            try:
                if float(ovr_btn) > 1 and float(btn) < 0.25:
                    btn_adj.append(str(float(btn) + float(ovr_btn)))
                else:
                    btn_adj.append(ovr_btn)
            except ValueError:
                btn_adj.append(ovr_btn)

        winning_time = self.get_winning_time()

        if winning_time is None:
            return ['-' for _ in range(int(self.race_info.ran))]

        return self.calculate_times(
            winning_time,
            btn_adj,
            self.race_info.going,
            self.race_info.race_type,
        )

    def get_headgear(self, runner: dict) -> str:
        headgear = runner.get('headgear')

        if not headgear:
            return ''

        return headgear + ('1' if runner.get('isFirstTimeHeadgear') else '')

    def get_lbs(self, runner: dict) -> str:
        lbs = runner.get('weightCarriedLbs')

        if lbs is not None:
            return str(lbs)

        stones = runner.get('weightStones')
        pounds = runner.get('weightPounds')

        if stones is None or pounds is None:
            return ''

        return str(int(stones) * 14 + int(pounds))

    def get_position(self, runner: dict) -> str:
        if runner.get('isDisqualified'):
            return 'DSQ'

        return str(runner.get('outcomeCode') or '')

    def get_prize(self, pos: str, prizes: dict[int, str]) -> str:
        if not pos.isnumeric():
            return ''

        prize = prizes.get(int(pos))

        if not prize:
            return ''

        return prize.replace('£', '').replace(',', '').strip()

    def get_rating(self, value: str | None) -> str:
        rating = (value or '').strip()
        return rating if rating.isdigit() else ''

    def get_race_class(self, code: str) -> str:
        if code:
            return f'Class {code}'

        classes = {
            'a': '1',
            'b': '2',
            'c': '3',
            'd': '4',
            'e': '5',
            'f': '6',
            'g': '6',
            'h': '7',
        }

        match = search(regex_class, self.race_info.race_name)

        if match:
            race_class = match.groups()[2].lower()
            if race_class in classes:
                return 'Class ' + classes[race_class]
            return 'Class ' + race_class

        if '(premier handicap)' in self.race_info.race_name:
            return 'Class 2'

        if self.race_info.pattern:
            return 'Class 1'

        return ''

    def get_race_distances(self, header: dict) -> tuple[str, str, str, str]:
        dist = header.get('distanceShort') or ''

        try:
            dist_f = distance_to_furlongs(dist)
        except ValueError:
            print('ERROR: distance_to_furlongs()')
            print('Race: ', self.data.get('raceId'))
            sys.exit()

        dist_m = distance_to_metres(header.get('distanceFull') or '')

        if dist_m == 0:
            dist_m = round(dist_f * 201.168)

        dist_y = round(dist_m * 1.0936)
        dist_f = str(dist_f).replace('.0', '') + 'f'

        if self.race_info.region not in {'GB', 'IRE', 'USA', 'CAN'}:
            dist_m = float(dist_f.strip('f')) * 200

        return dist, str(int(dist_y)), dist_f, str(int(dist_m))

    def get_race_pattern(self) -> str:
        match = search(regex_group, self.race_info.race_name)

        if match:
            pattern = f'{match.groups()[1]} {match.groups()[4]}'.title()
            return pattern.title()

        if 'Forte Mile' in self.race_info.race_name and '(Group' in self.race_info.race_name:
            return 'Group 2'

        if any(x in self.race_info.race_name.lower() for x in {'listed race', '(listed'}):
            return 'Listed'

        return ''

    def get_race_type(self, code: str) -> str:
        return RACE_TYPE_CODES.get(code, 'Flat')

    def get_sex(self, pedigree: dict) -> str:
        parts = (pedigree.get('colourSex') or '').split()

        if not parts:
            return ''

        return parts[-1].upper()

    def get_weight(self, runner: dict) -> str:
        stones = runner.get('weightStones')
        pounds = runner.get('weightPounds')

        if stones is None or pounds is None:
            return ''

        return f'{stones}-{pounds}'

    def get_winning_time(self) -> float | None:
        raw = (self.data['details'].get('winningTime') or '').strip()

        if not raw:
            return None

        parts = raw.split()

        try:
            if len(parts) == 2:
                return round(float(parts[0].rstrip('m')) * 60 + float(parts[1].rstrip('s')), 2)
            return round(float(parts[0].rstrip('s')), 2)
        except ValueError:
            return None

    def id_from_url(self, url: str | None) -> str:
        if not url:
            return ''

        parts = url.split('/')
        return parts[3] if len(parts) > 3 else ''

    def join_betfair_data(self, bsp_map: BSPMap):
        key = (self.race_info.region, self.race_info.date, self.race_info.off)
        bsp = bsp_map.get(key)

        self.runner_info.set_bsp_list_width(len(self.runner_info.horse))

        if not bsp:
            return

        for i, horse in enumerate(self.runner_info.horse):
            name = horse.split('(')[0].strip().lower()
            for row in bsp:
                if jarowinkler_similarity(name, row.horse) >= 0.77:
                    self.runner_info.bsp[i] = row.bsp or ''
                    self.runner_info.pre_min[i] = row.pre_min or ''
                    self.runner_info.pre_max[i] = row.pre_max or ''
                    self.runner_info.ip_min[i] = row.ip_min or ''
                    self.runner_info.ip_max[i] = row.ip_max or ''
                    self.runner_info.pre_vol[i] = row.pre_vol or ''
                    self.runner_info.ip_vol[i] = row.ip_vol or ''
                    break

    def name_with_region(self, name: str | None, region: str | None) -> str:
        name = clean_string(name or '')

        if not name:
            return ''

        region = (region or 'GB').strip()
        if not region.startswith('('):
            region = f'({region})'

        return f'{name} {region}'

    def parse_race_bands(self, band: str) -> tuple[str, str]:
        bands = band.strip('()').split(',')

        band_age = ''
        band_rating = ''

        if len(bands) > 1:
            for x in bands:
                if 'yo' in x:
                    band_age = x.strip()
                elif '-' in x:
                    band_rating = x.strip()
        else:
            if 'yo' in band:
                band_age = band.strip()
            elif '-' in band:
                band_rating = band.strip()

        return band_age.strip('()'), band_rating

    def sex_restricted(self) -> str:
        race_name = self.race_info.race_name.lower()

        patterns = [
            (['entire colts & fillies', 'colts & fillies'], 'C & F'),
            (['fillies & mares', 'filles & mares'], 'F & M'),
            (['colts & geldings', 'colts/geldings', '(c & g)'], 'C & G'),
            (['(mares & geldings)'], 'M & G'),
            (['fillies'], 'F'),
            (['mares'], 'M'),
        ]

        for terms, result in patterns:
            if any(term in race_name for term in terms):
                return result

        return ''

    def time_to_seconds(self, times: list[str]) -> list[str]:
        def convert_time(time_str: str) -> str:
            if time_str in {'-', ''}:
                return time_str
            try:
                mins, secs = time_str.split(':')
                total_seconds = (int(mins) * 60) + float(secs)
                return f'{total_seconds:.2f}'
            except ValueError:
                raise ValueError(f"Invalid time format: '{time_str}' from {self.data.get('raceId')}")

        return [convert_time(t) for t in times]


def distance_to_decimal(dist: str):
    return (
        dist.strip()
        .replace('¼', '.25')
        .replace('½', '.5')
        .replace('¾', '.75')
        .replace('snk', '0.2')
        .replace('nk', '0.3')
        .replace('sht-hd', '0.1')
        .replace('shd', '0.1')
        .replace('hd', '0.2')
        .replace('nse', '0.05')
        .replace('dht', '0')
        .replace('dist', '30')
    )


def distance_to_furlongs(distance: str):
    dist = ''.join(
        [d.strip().replace('¼', '.25').replace('½', '.5').replace('¾', '.75') for d in distance]
    )

    if 'm' in dist:
        if len(dist) > 2:
            dist = int(dist.split('m')[0]) * 8 + float(dist.split('m')[1].strip('f'))
        else:
            dist = int(dist.split('m')[0]) * 8
    else:
        dist = dist.strip('f')

    return float(dist)


def distance_to_metres(distance: str) -> int:
    dist = distance.lower()
    metres = 0

    if 'm' in dist:
        metres += int(dist.split('m')[0]) * 1609.34

    if 'f' in dist:
        metres += int(dist.split('f')[0][-1]) * 201.168

    if 'yds' in dist:
        if 'f' in dist:
            metres += int(dist.split('f')[1].strip('yds')) * 0.914
        elif 'm' in dist:
            metres += int(dist.split('m')[1].strip('yds')) * 0.914

    return round(metres)


def fraction_to_decimal(fractions: list[str]) -> list[str]:
    decimal: list[str] = []

    for fraction in fractions:
        if fraction in {'', 'No Odds', '&'}:
            decimal.append('')
        elif fraction.lower() in {'evens', 'evs'}:
            decimal.append('2.00')
        else:
            num, den = fraction.split('/')
            decimal.append(f'{float(num) / float(den) + 1.00:.2f}')

    return decimal
