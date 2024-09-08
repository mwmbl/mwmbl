import { redirect } from "@sveltejs/kit";

export async function load({ url }) {
    const query = url.searchParams.get("q")
    if (url.searchParams.get("q") != null) {
        redirect(303, '/search?q='+query);
    }
}