"""KI-17: junk images (energy labels, nav/mega-menu tiles, campaign tiles,
generated og:image text cards) must not pass the image filter, and the real
product photos of the same rows must still pass. Offline - no network.

    .venv\\Scripts\\python.exe -m pytest tests\\test_junk_images_ki17.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sku_enricher as E                          # noqa: E402

# evidence from runs\mixed-2026-11-20\mixed_filled.xlsx
JUNK = [
    # SAMSUNG-AR50H19D1PHNNA: energy labels
    "https://images.samsung.com/is/image/samsung/p6pim/in/ar50h19d1phnna/energylabel/"
    "in-energylabel-product-ar50h19d1phnna-554958867?$Q90_2052_1641_JPG$",
    "https://images.samsung.com/is/image/samsung/p6pim/in/ar50h19d1phnna/energylabel/"
    "in-energylabel-product-ar50h19d1phnna-554958867?$1164_776_PNG$",
    # HAIER-HRD-2561SGSA-N: bajajfinserv.in mega-menu banners
    "https://cms-assets.bajajfinserv.in/is/image/bajajfinance/mega1?scl=1",
    "https://cms-assets.bajajfinserv.in/is/image/bajajfinance/mega2?scl=1",
    "https://cms-assets.bajajfinserv.in/is/image/bajajfinance/BM_Explore_tractors?scl=1&fmt=png-alpha",
    # LG-US-Q18ENXE: lg.com global navigation bar tile
    "https://www.lg.com/content/dam/channel/wcms/in/gnb/windowsac-644x328.jpg",
    # VOLTAS BEKO-WFL7012VTMP: generated text card
    "https://bestpurifier.in/og?t=Voltas+Beko+7+kg+5+Star+Front+Load+Washing+Machine"
    "+Price+in+India&k=Review&i=%2Fimages%2Fwashers%2Fvoltas-beko-wfl7012vtmp-7-0kg.png",
    # PANASONIC-CW-XU185CGT: campaign tile
    "https://www.panasonic.com/content/dam/common/Global/get-inspired-section/"
    "air-conditioners/2026/kick-off-ac-26-listing/Higlish.jpg",
    # same patterns, other spellings
    "https://example.com/media/energy-label/fridge-xyz.png",
    "https://example.com/img/home/mega3.jpg",
    "https://example.com/media/catalog/product/energy_label.jpg",
    # round 2: junk that moved into the freed slots (runs\ki17-replay)
    "https://resources.samsung.com/etc.clientlibs/samsung/clientlibs/consumer/global/"
    "clientlib-common/resources/images/03-coverage-software-coverage.png",
    "https://resources.samsung.com/etc.clientlibs/samsung/clientlibs/consumer/global/"
    "clientlib-common/resources/images/03_coverage_waterdamage.png",
    "https://resources.samsung.com/etc.clientlibs/samsung/clientlibs/consumer/global/"
    "clientlib-common/resources/images/upgrade-popup-condition-icon-01.png",
    "https://www.lg.com/content/dam/channel/wcms/in/split-ac/pictograms/"
    "new-version-pictogram/new/333333.jpg",
    "https://www.bajajfinserv.in/content/dam/bajajmall-site/dam/HostedLink/220126/og_tag.png",
    # round 3: bazaarvoice badge and lg.com feature tiles (runs\ki17-replay2)
    "https://apps.bazaarvoice.com/common/images/intelligent_trustmark_icons/trustmark_en.png",
    "https://www.lg.com/content/dam/channel/wcms/in/images/split-ac/features/new/mzzzzz.jpg",
    "https://www.lg.com/content/dam/channel/wcms/in/images/split-ac/features/new/2025/M-Diet-Mode-2.jpg",
    "https://www.lg.com/content/dam/channel/wcms/in/images/split-ac/features/new/2025/Cools-at-55°-C-2.jpg",
    # round 4: watermark and social share images (runs\ki17-replay3)
    "https://static3.industrybuying.com/a-wk/images/watermark.jpg",
    "https://rakeshretails.com/opengraph-image.jpeg?6ae76f3a28f9f9cb",
    "https://rakeshretails.com/twitter-image.jpeg?6ae76f3a28f9f9cb",
]

KEEP = [
    # the good photos of the same rows
    "https://www.lg.com/content/dam/channel/wcms/in/split-ac/gallery/offers/"
    "US-Q18ENXE-2010X1334.jpg/jcr:content/renditions/thum-1600x1062.jpeg?w=800",
    "https://www.lg.com/content/dam/channel/wcms/in/split-ac/gallery/offers/US-Q18ENXE-450X450.jpg",
    "https://panasonic.com/content/dam/panasonic/in/en/pim/dam/CW/CW-XU1/CW-XU185CGT/ast-3117317.jpg",
    # look-alikes that are real product photos
    "https://example.com/media/catalog/product/megafresh-refrigerator-front.jpg",
    "https://example.com/media/catalog/product/explorer-trolley-bag-front.jpg",
    "https://example.com/media/catalog/product/energy-efficient-inverter-ac-front.jpg",
    "https://example.com/images/og/washing-machine-front.jpg",
    "https://example.com/images/omega-mixer-grinder-500w.jpg",
    "https://example.com/images/products/mega-kitchen-chimney-90cm.jpg",
    # round 2 look-alikes
    "https://images.samsung.com/is/image/samsung/p6pim/in/ar50h19d1phnna/gallery/"
    "in-split-ac-ar50h19d1phnna-front-white-554958870?$1300_1038_PNG$",
    "https://www.lg.com/content/dam/channel/wcms/in/split-ac/gallery/offers/US-Q18ENXE-2010X1334.jpg",
    "https://mc.bajajfinserv.in/media/catalog/product/h/r/hrd2561sgsan_base.jpeg",
    "https://example.com/media/catalog/product/high-voltage-stabilizer-front.jpg",
    "https://example.com/images/products/price-tag-free-kettle.jpg",
    "https://example.com/images/products/blog-tagine-pot.jpg",
    # round 3: '/features/' is junk only on lg.com
    "https://example.com/media/catalog/product/features/ac-front.jpg",
    "https://www.lg.com/content/dam/channel/wcms/in/images/split-ac/gallery/US-Q18ENXE-front.jpg",
    # round 4 look-alikes
    "https://example.com/media/catalog/product/water-purifier-ro.jpg",
    "https://example.com/media/catalog/product/open-door-view.jpg",
]


@pytest.mark.parametrize("url", JUNK)
def test_evidence_url_is_junk(url):
    assert E.IMAGE_JUNK_RE.search(url.lower())
    assert not E.looks_like_product_image(url)


@pytest.mark.parametrize("url", KEEP)
def test_real_photo_is_kept(url):
    assert not E.IMAGE_JUNK_RE.search(url.lower())
    assert E.looks_like_product_image(url)


def test_dedupe_drops_junk_keeps_photo():
    out = E.dedupe_images([
        "https://www.lg.com/content/dam/channel/wcms/in/gnb/windowsac-644x328.jpg",
        "https://panasonic.com/content/dam/panasonic/in/en/pim/dam/CW/CW-XU1/CW-XU185CGT/ast-3117317.jpg",
    ])
    assert out == ["https://panasonic.com/content/dam/panasonic/in/en/pim/dam/"
                   "CW/CW-XU1/CW-XU185CGT/ast-3117317.jpg"]
