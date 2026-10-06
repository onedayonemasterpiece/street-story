from street_story.article_media import extract_media


def test_mixed_article_context_binds_each_descriptor_to_its_own_figure_and_section():
    title, descriptors = extract_media('''<h1>Article title names Gate A</h1><article>
      <section><h2>Gate A</h2><p>Eastern facade of the first building.</p>
        <figure><a href="/first.jpg"><img src="/first-preview.jpg" alt="Publisher guess"></a>
          <figcaption>First illustration caption</figcaption></figure></section>
      <section><h2>Gate B</h2><p>Rear entrance of the second building.</p>
        <figure><img srcset="/second-small.jpg 300w, /second.jpg 1200w" alt="Gate A">
          <figcaption>Second illustration caption</figcaption></figure></section>
      </article>''', 'https://history.example/mixed')
    assert title == 'Article title names Gate A'
    first, second = descriptors
    assert first['image_url'] == 'https://history.example/first.jpg'
    assert second['image_url'] == 'https://history.example/second.jpg'
    assert first['figcaption'] == 'First illustration caption'
    assert second['figcaption'] == 'Second illustration caption'
    assert first['section_heading'] == 'Gate A'
    assert second['section_heading'] == 'Gate B'
    assert first['context_text'] == 'Eastern facade of the first building.'
    assert second['context_text'] == 'Rear entrance of the second building.'
    assert second['alt'] == 'Gate A'  # Preserve conflicting publisher context as data.
    assert all(d['article_url'] == 'https://history.example/mixed' for d in descriptors)
    assert all(not {'candidate_id', 'subject_candidate_id', 'identity_eligible', 'confidence'} & d.keys()
               for d in descriptors)


def test_media_context_is_bounded_and_does_not_leak_from_another_section_or_article():
    _title, descriptors = extract_media('''<h2>Unrelated navigation heading</h2>
      <article><section><h2>''' + 'A' * 250 + '''</h2><p>''' + 'B' * 500 + '''</p>
        <figure><img src="/first.jpg"><figcaption>''' + 'C' * 450 + '''</figcaption></figure>
      </section><section><img src="/second.jpg"></section></article>
      <article><img src="/third.jpg"></article>''', 'https://history.example/mixed')
    assert len(descriptors) == 3
    first, second, third = descriptors
    assert first['section_heading'] == 'A' * 180
    assert first['context_text'] == 'B' * 360
    assert first['figcaption'] == 'C' * 300
    assert all(key not in second and key not in third
               for key in ('section_heading', 'context_text', 'figcaption'))
