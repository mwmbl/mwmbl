export async function load({ url }) {
	const response = await fetch('https://mwmbl.org/search/?s=' + url.searchParams.get('q'));
	const results: Array<{
		title: Array<{ value: string; is_bold: boolean }>;
		extract: Array<{ value: string; is_bold: boolean }>;
		url: string;
		source: string;
	}> = await response.json();

	return {
		query: url.searchParams.get('q') as string | undefined,
		results: results
	};
}
