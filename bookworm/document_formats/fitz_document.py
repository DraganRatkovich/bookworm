# coding: utf-8

import zipfile
import fitz
import ftfy
from functools import cached_property
from hashlib import md5
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from pathlib import Path
from bookworm.paths import home_data_path
from bookworm.image_io import ImageIO
from bookworm.utils import recursively_iterdir
from bookworm.document_formats.base import (
    BaseDocument,
    BasePage,
    Section,
    BookMetadata,
    Pager,
    DocumentCapability as DC,
    ChangeDocument,
    DocumentError,
    DocumentEncryptedError,
)
from bookworm.logger import logger


log = logger.getChild(__name__)


class FitzPage(BasePage):
    """Wrapps fitz.Page."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fitz_page = self.document._ebook[self.index]

    def _text_from_page(self, page: fitz.Page) -> str:
        bloks = page.getTextBlocks()
        text = [blk[4].replace("\n", " ") for blk in bloks if blk[-1] == 0]
        text = "\r\n".join(text)
        return ftfy.fix_text(text, normalization="NFKC")

    def get_text(self):
        return self._text_from_page(self._fitz_page)

    def get_image(self, zoom_factor=1.0):
        mat = fitz.Matrix(zoom_factor, zoom_factor)
        pix = self._fitz_page.getPixmap(matrix=mat, alpha=True)
        return ImageIO(data=pix.samples, width=pix.width, height=pix.height)


class FitzDocument(BaseDocument):
    """The backend of this document type is Fitz (AKA MuPDF)."""

    format = None
    # Translators: the name of a document file format
    name = None
    extensions = ()
    capabilities = (
        DC.TOC_TREE | DC.METADATA | DC.GRAPHICAL_RENDERING | DC.IMAGE_EXTRACTION
    )

    def get_page(self, index: int) -> FitzPage:
        return FitzPage(self, index)

    def __len__(self) -> int:
        return self._ebook.pageCount

    def read(self, filetype=None):
        self.filename = self.get_file_system_path()
        try:
            self._ebook = fitz.open(self.filename, filetype=filetype)
            super().read()
        except RuntimeError as e:
            log.exception("Failed to open document", exc_info=True)
            if "drm" in e.args[0].lower():
                raise DocumentEncryptedError("Document is encrypted with DRM") from e
            raise DocumentError("Could not open document") from e

    def close(self):
        if self._ebook is None:
            return
        self._ebook.close()
        self._ebook = None
        super().close()

    def is_encrypted(self):
        return bool(self._ebook.isEncrypted)

    def decrypt(self, password):
        return bool(self._ebook.authenticate(password))

    @cached_property
    def toc_tree(self):
        toc_info = self._ebook.getToC(simple=False)
        max_page = len(self) - 1
        root_item = Section(
            document=self,
            title=self.metadata.title,
            pager=Pager(first=0, last=max_page),
            data={"html_file": None},
        )
        _last_entry = None
        for (index, (level, title, start_page, infodict)) in enumerate(toc_info):
            try:
                curr_index = index
                next_item = toc_info[curr_index + 1]
                while next_item[0] != level:
                    curr_index += 1
                    next_item = toc_info[curr_index]
            except IndexError:
                next_item = None
            first_page = start_page - 1
            last_page = max_page if next_item is None else next_item[2] - 2
            if first_page < 0:
                first_page = 0 if _last_entry is None else _last_entry.pager.last
            if last_page < first_page:
                last_page += 1
            if not all(p >= 0 for p in (first_page, last_page)):
                continue
            if first_page > last_page:
                continue
            pgn = Pager(first=first_page, last=last_page)
            sect = Section(
                document=self,
                title=title,
                pager=pgn,
                data={"html_file": infodict.get("name")},
            )
            if level == 1:
                root_item.append(sect)
                _last_entry = sect
                continue
            elif not root_item:
                continue
            parent = root_item.children[-1]
            parent_lvl = level - 1
            while True:
                if (parent_lvl > 1) and parent.children:
                    parent = parent.children[-1]
                    parent_lvl -= 1
                    continue
                parent.append(sect)
                _last_entry = sect
                break
        return root_item

    @cached_property
    def metadata(self):
        meta = self._ebook.metadata
        to_str = (
            lambda value: "" if value is None else ftfy.fix_text_encoding(value).strip()
        )
        return BookMetadata(
            title=to_str(meta["title"]) or Path(self.filename).stem,
            author=to_str(meta["author"]),
            publication_year=to_str(meta["creationDate"]),
        )
