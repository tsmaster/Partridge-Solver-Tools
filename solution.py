from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _brightness(color):
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def _text_color_for(background):
    """Black on light tiles, white on dark ones, so the size label stays readable across the
    whole palette instead of a single fixed color (same formula watch_solution.py's terminal
    digit-overlay already uses for the same reason)."""
    return (0, 0, 0) if _brightness(background) > 140 else (255, 255, 255)

class PieceLocation:
    def __init__(self, x, y, sz):
        self.x = x
        self.y = y
        self.sz = sz

class Solution:
    def __init__(self):
        self.piece_locations = []
        self.hash = None

    def sort_locations_by_raster(self):
        self.piece_locations = sorted(self.piece_locations, key = lambda loc: (loc.y, loc.x))

    def sort_locations_by_size(self):
        self.piece_locations = sorted(self.piece_locations, key = lambda loc: loc.sz)
    
    def to_json(self):
        pass

    def get_hash(self):
        if not self.hash:
            self.sort_locations_by_raster()
            h = ""
            for loc in self.piece_locations:
                ls = str(loc.sz)
                h = h + ls
            self.hash = h

        return self.hash

    def save_to_png(self, filename, piece_width=10, border_width=2):
        img_width = piece_width*45 + border_width
        img = Image.new("RGB", (img_width, img_width))
        draw = ImageDraw.Draw(img)

        colors= {1: (128, 128, 128), # gray / translucent
                 2: (80, 64, 0),     # chocolate brown
                 3: (92, 0, 92),     # purple
                 4: (0, 0, 192),     # blue
                 5: (0, 192, 0),     # green
                 6: (192, 192, 0),   # yellow
                 #7: (192, 0, 0),     # red
                 #8: (192, 80, 0),    # orange
                 7: (255, 140, 0),   # orange (darkorange - was too close to 8's red before)
                 8: (192, 0, 0),     # red
                 9: (192, 192, 192)} # light gray (white?)
        

        for piece_loc in self.piece_locations:
            draw.rectangle((piece_loc.x*piece_width,
                            piece_loc.y*piece_width,
                            (piece_loc.x+piece_loc.sz) * piece_width,
                            (piece_loc.y+piece_loc.sz) * piece_width),
                           fill=colors[piece_loc.sz])

        for piece_loc in self.piece_locations:
            draw.rectangle((piece_loc.x*piece_width,
                            piece_loc.y*piece_width,
                            (piece_loc.x+piece_loc.sz) * piece_width,
                            (piece_loc.y+piece_loc.sz) * piece_width),
                           fill = None,
                           outline = (0,0,0),
                           width = border_width)

        # Size label centered in each tile, in a color chosen for contrast against that tile's own
        # fill - helps distinguish similar-looking colors (e.g. orange vs red) at a glance, and
        # especially helps readers with reduced color vision, for whom color alone isn't reliable.
        for piece_loc in self.piece_locations:
            tile_px = piece_loc.sz * piece_width
            font_size = max(1, round(tile_px * 0.6))
            try:
                font = ImageFont.truetype(FONT_PATH, font_size)
            except OSError:
                font = ImageFont.load_default()
            text = str(piece_loc.sz)
            center_x = piece_loc.x * piece_width + tile_px / 2
            center_y = piece_loc.y * piece_width + tile_px / 2
            draw.text((center_x, center_y), text, fill=_text_color_for(colors[piece_loc.sz]),
                      font=font, anchor="mm")

        img.save(filename)
        
        


def find_locations_in_lines(lines):
    for y in range(45):
        for x in range(45):
            if lines[y][x] == '+':
                if ((x < 44) and
                    (y < 44) and
                    (lines[y][x+1] == '-') and
                    (lines[y+1][x] == '|')):
                    sz = int(lines[y+1][x+1])
                else:
                    sz = 1
                #print("found corner", x, y, sz)
                pc_loc = PieceLocation(x,y,sz)
                yield pc_loc

def make_solution_from_lines(lines):
    s = Solution()

    for loc in find_locations_in_lines(lines):
        s.piece_locations.append(loc)

    s.sort_locations_by_raster()
    
    # TODO

    return s
