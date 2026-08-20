// Links report — expand/collapse facet lists in place.
//
// The "More formats" toggle is a real link (?formats=all) so it works with
// JS off; when JS is available we intercept the click and show/hide the
// extra items without a page load.
//
// Works for any number of toggles: each .facet-toggle lives inside the
// .facet-list it controls (as its last <li>), and the items beyond the
// visible cut-off carry the .facet-extra class.

document.querySelectorAll('.facet-toggle').forEach((toggle) => {
	toggle.addEventListener('click', (event) => {
		event.preventDefault()

		const list = toggle.closest('.facet-list')
		const expanded = list.classList.toggle('facet-list--expanded')

		toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false')
		toggle.textContent = expanded ? toggle.dataset.fewerLabel : toggle.dataset.moreLabel

		// Keep the expanded state in the query string of the facet links and the
		// toggle itself, so clicking a facet (or opening in a new tab) doesn't
		// collapse the list back to the top 10.
		const param = toggle.dataset.param
		const value = toggle.dataset.value
		if (param && value) {
			const updaters = [toggle, ...document.querySelectorAll('.facet-link')]
			for (const a of updaters) {
				const url = new URL(a.getAttribute('href'), window.location.href)
				if (expanded) url.searchParams.set(param, value)
				else url.searchParams.delete(param)
				a.setAttribute('href', url.pathname + url.search + url.hash)
			}
		}
	})
})
