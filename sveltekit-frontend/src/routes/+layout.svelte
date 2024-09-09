<script>
	import '../app.css';

	import { Button } from '@/components/ui/button';
	import * as Sheet from '@/components/ui/sheet';
	import CiHamburgerMd from '~icons/ci/hamburger-md'
</script>

<div class="flex min-h-screen flex-col">
	<div class="border-t-8 border-pink-400"></div>
	<header
		class="flex h-16 w-full max-w-[80rem] flex-row items-center gap-2 self-center pb-8 px-5 lg:px-8 pt-10 sm:gap-6"
	>
		<a href="/" class="mr-auto"><h1>mwmbl</h1></a>
		<div class="hidden md:contents">
			<Button variant="link" href="/" class="px-2">Home</Button>
			<Button variant="link" href="/" class="px-2">Our Mission</Button>
			<Button variant="link" href="/" class="px-2">The Team</Button>
			<Button variant="link" href="https://opencollective.com/mwmbl" class="px-2">Donate</Button>
		</div>
		<div class="md:hidden contents">
			<Sheet.Root>
				<Sheet.Trigger asChild let:builder>
					<Button builders={[builder]} variant="outline" size="icon">
						<CiHamburgerMd class="w-7 h-7"/>
					</Button>
				</Sheet.Trigger>
				<Sheet.Content side="right">
				  <Sheet.Header>
					<Sheet.Title>Mwmbl Menu</Sheet.Title>
				  </Sheet.Header>
				  <ol class="flex flex-col items-start">
					<Button variant="link" href="/" class="px-2">Home</Button>
					<Button variant="link" href="/" class="px-2">Our Mission</Button>
					<Button variant="link" href="/" class="px-2">The Team</Button>
					<Button variant="link" href="https://opencollective.com/mwmbl" class="px-2">Donate</Button>
				  </ol>
				</Sheet.Content>
			  </Sheet.Root>
		</div>
	</header>
	<slot></slot>
</div>
