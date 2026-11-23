"""KI-18: an image whose file name names a DIFFERENT model is another
product's photo and must be dropped; the row's own photos, shared-prefix
variants and hash-named CDN files must stay. Offline - no network.

    .venv\\Scripts\\python.exe -m pytest tests\\test_wrong_model_images_ki18.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sku_enricher as E                          # noqa: E402

@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(E, "log", lambda *a, **k: None)


@pytest.mark.parametrize("model,core", [
    ("HRD-2561SGSA-N", "HRD2561"),
    ("GLD2256ZARS", "GLD2256"),
    ("US-Q18ENXE", "USQ18"),
    ("AR50H19D1PHNNA", "AR50"),
    ("CW-XU185CGT", "CWXU185"),
    ("A12", ""),                 # too short to tell models apart: not judged
    ("6110", ""),                # no leading letters: not judged
    ("", ""),
])
def test_model_core(model, core):
    assert E.model_core(model) == core


FOREIGN = [
    ("GLD2256ZARS",
     "https://www.lg.com/content/dam/channel/wcms/in/images/refrigerators/"
     "gl-d201ascu_asczebn_eail_in_c/gallery/GL-D201ASCU-Refrigerators-Front-View-D-01-v.jpg"),
    ("HRD-2561SGSA-N",
     "https://arihantelectronics.org/wp-content/uploads/2026/09/glds192acwcf_main.jpg"),
    ("HRD-2561SGSA-N",
     "https://arihantelectronics.org/wp-content/uploads/2026/09/gldg192abscf_m.jpg"),
    ("HRD-2561SGSA-N",
     "https://arihantelectronics.org/wp-content/uploads/2026/09/gldg192afadf_m-324x324.jpg?crop=1"),
    # correct drops the review confirmed
    ("GLD2256ZARS", "https://example.com/images/samsung-ar1234.jpg"),
    ("GLD2256ZARS", "https://example.com/images/NR-B1234.jpg"),
    ("GLD2256ZARS", "https://example.com/images/model-tl1250.jpg"),
]

KEEP = [
    ("HRD-2561SGSA-N",           # shared-prefix variant of the row's model
     "https://arihantelectronics.org/wp-content/uploads/2026/10/haier-hrd-2561sga-n.jpg"),
    ("HRD-2561SGSA-N",
     "https://mc.bajajfinserv.in/media/catalog/product/h/r/hrd2561sgsan_base.jpeg"),
    ("US-Q18ENXE",
     "https://www.lg.com/content/dam/channel/wcms/in/split-ac/gallery/offers/"
     "US-Q18ENXE-2010X1334.jpg/jcr:content/renditions/thum-1600x1062.jpeg?w=800"),
    ("US-Q18ENXE",
     "https://www.lg.com/content/dam/channel/wcms/in/split-ac/gallery/offers/US-Q18ENXE-450X450.jpg"),
    ("CW-XU185CGT",
     "https://panasonic.com/content/dam/panasonic/in/en/pim/dam/CW/CW-XU1/CW-XU185CGT/ast-3117317.jpg"),
    ("WA80F10B3NTL",
     "https://cdn.jiostore.online/v2/jmd-asp/jdprod/wrkr/products/pictures/item/free/original/"
     "samsung/494510487/0/M6AiRrdkSh-s8tfHTLIaT-Samsung-FATL-Washing-Machine-494510487-i-1-1200Wx1200H.jpeg"),
    ("AR50H19D1PHNNA",
     "https://images.samsung.com/is/image/samsung/p6pim/in/ar50h19d1phnna/gallery/"
     "in-split-ac-ar50h19d1phnna-front-white-554958870?$1300_1038_PNG$"),
    ("GLD2256ZARS", "https://m.media-amazon.com/images/I/71uNmwWDcZL.jpg"),
    ("GLD2256ZARS", "https://rukminim2.flixcart.com/image/832/832/xif0q/refrigerator/abc/x/y/"
                    "-original-imah2gzfhgfgx3zz.jpeg"),
    # Shopify-style uuid on a store's own domain (seen on darlingretail.com)
    ("UA32T4350BKXXL",
     "https://darlingretail.com/cdn/shop/products/1_7853cb40-0f63-43cb-932e-a66ca8d575f2.jpg?v=1755761712"),
    ("UA32T4350BKXXL",
     "https://cdn.shopify.com/s/files/1/0001/products/7853cb40-0f63-43cb-932e-a66ca8d575f2.jpg"),
    # a brand + year file name is not a model code
    ("HIL1901MBPB", "https://image.haier.com/in/images/haier2019_3-1.png"),
    # camera / CMS words with digits
    ("HRD-2561SGSA-N", "https://example.com/images/IMG_1234.jpg"),
    ("HRD-2561SGSA-N", "https://example.com/images/DSC01234.jpg"),
    ("HRD-2561SGSA-N", "https://example.com/images/size1500-front.jpg"),
    ("HRD-2561SGSA-N", "https://example.com/images/product-1500x1500.jpg"),
    # no model-shaped code at all
    ("HRD-2561SGSA-N", "https://image.haier.com/in/refrigerators/W020210325401344362994.jpg"),
    # review D1: retailer size tokens are not model codes
    ("RT28C3053S8",
     "https://www.reliancedigital.in/medias/Samsung-Refrigerator-494351006-i-1-1200Wx1200H?context=x"),
    ("RT28C3053S8",
     "https://img.tatacliq.com/images/i11/437Wx649H/MP000000012345678_437Wx649H_202401010101.jpeg"),
    # review D2: file-type / CMS words, and brand + bare digits
    ("GLD2256ZARS", "https://example.com/images/SKU12345.jpg"),
    ("GLD2256ZARS", "https://example.com/images/WEBP1500.jpg"),
    ("GLD2256ZARS", "https://example.com/images/PNG1500.jpg"),
    ("GLD2256ZARS", "https://example.com/images/MAX1500.jpg"),
    ("GLD2256ZARS", "https://example.com/images/SLR1500.jpg"),
    ("GLD2256ZARS", "https://example.com/images/LGE-1000PX.jpg"),
    ("GLD2256ZARS", "https://example.com/images/BLDC1500-front.jpg"),
    ("GLD2256ZARS", "https://example.com/images/lg-1500.jpg"),
    # review P1: a word glued to a letter-led segment is not a model prefix
    ("GLD2256ZARS", "https://example.com/images/front-view-d001.jpg"),
    ("GLD2256ZARS", "https://example.com/images/side-view-a123.jpg"),
    ("GLD2256ZARS", "https://example.com/images/hero-e1234.jpg"),
    ("SA-12345", "https://example.com/images/lifestyle-shot-k1200.jpg"),
    ("GLD2256ZARS", "https://example.com/images/new-a1234.jpg"),
    ("GLD2256ZARS", "https://example.com/images/top-view-d001.jpg"),
    # correct keeps the review confirmed
    ("GLD2256ZARS", "https://example.com/images/image-b1500.jpg"),
    ("GLD2256ZARS", "https://example.com/images/product-s1000.jpg"),
    ("GLD2256ZARS", "https://example.com/images/new-model-x1500.jpg"),
    ("GLB201", "https://example.com/images/lg-gl-b201.jpg"),
]


@pytest.mark.parametrize("model,url", FOREIGN)
def test_foreign_model_image_dropped(model, url):
    assert E.foreign_model_image(url, E.model_core(model))


@pytest.mark.parametrize("model,url", KEEP)
def test_own_or_unjudgeable_image_kept(model, url):
    assert not E.foreign_model_image(url, E.model_core(model))


def test_drop_foreign_model_images_on_record():
    rec = {"image_url": ",".join([
        "https://arihantelectronics.org/wp-content/uploads/2026/10/haier-hrd-2561sga-n.jpg",
        "https://arihantelectronics.org/wp-content/uploads/2026/09/glds192acwcf_main.jpg",
        "https://arihantelectronics.org/wp-content/uploads/2026/09/gldg192abscf_m.jpg",
    ])}
    E.drop_foreign_model_images(rec, "HRD-2561SGSA-N")
    assert rec["image_url"] == ("https://arihantelectronics.org/wp-content/"
                                "uploads/2026/10/haier-hrd-2561sga-n.jpg")


def test_reliance_size_named_gallery_all_kept():
    urls = [f"https://www.reliancedigital.in/medias/Samsung-Refrigerator-494351006-i-{n}"
            f"-1200Wx1200H?context=x" for n in (1, 2, 3)]
    rec = {"image_url": ",".join(urls)}
    E.drop_foreign_model_images(rec, "RT28C3053S8")
    assert rec["image_url"] == ",".join(urls)


def test_only_image_foreign_leaves_zero_and_status_says_so():
    rec = {"image_url": "https://www.lg.com/content/dam/channel/wcms/in/images/refrigerators/"
                        "gl-d201ascu_asczebn_eail_in_c/gallery/GL-D201ASCU-Refrigerators-Front-View-D-01-v.jpg",
           "sale price": "20000"}
    E.drop_foreign_model_images(rec, "GLD2256ZARS")
    assert rec["image_url"] == ""
    assert " - only 0 image(s)" in E.status_for(rec, "https://www.lg.com/in/x/", 3)


def test_unjudgeable_model_changes_nothing():
    rec = {"image_url": "https://example.com/images/GLD2201-front.jpg"}
    E.drop_foreign_model_images(rec, "A12")
    assert rec["image_url"] == "https://example.com/images/GLD2201-front.jpg"
