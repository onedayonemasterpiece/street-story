from street_story.article_media import extract_media


def test_publisher_theme_layout_classes_do_not_hide_detail_images():
    title, images = extract_media('''<body class="header_nopacity header_fill_light footer-vcustom">
      <header><img src="/brand.jpg"></header>
      <div role="main" class="main banner-auto">
        <h1>Current project</h1><div class="detail projects">
          <a class="fancybox" href="/upload/current.jpg" data-fancybox-group="gallery">
            <img src="/upload/thumb.jpg" itemprop="image" alt="Current project"></a>
          <img data-src="/upload/rear.jpg">
          <div class="ad-banner"><img src="/promotion.jpg"></div>
          <aside><div class="gallery"><img src="/unrelated.jpg"></div></aside>
          <footer><img src="/footer.jpg"></footer>
        </div>
      </div></body>''', 'https://example.com/project/')
    assert title == 'Current project'
    assert [item['image_url'] for item in images] == [
        'https://example.com/upload/current.jpg', 'https://example.com/upload/rear.jpg']
    assert all(item['article_url'] == 'https://example.com/project/' for item in images)


def test_background_gallery_survives_theme_classes_but_nested_ad_does_not():
    _, images = extract_media('''<body class="header-theme footer-theme">
      <main class="banner-auto">
        <div class="detail"><div class="gallery" style="background:url('/project.jpg')"></div>
          <div class="ad-banner"><div class="gallery" style="background:url('/ad.jpg')"></div></div>
        </div>
      </main></body>''', 'https://example.com/project/')
    assert [item['image_url'] for item in images] == ['https://example.com/project.jpg']


def test_standalone_gallery_inside_sidebar_is_still_excluded():
    _, images = extract_media('''<body class="header-theme">
      <div class="sidebar"><div class="gallery"><img src="/related.jpg"></div></div>
    </body>''', 'https://example.com/project/')
    assert images == []
