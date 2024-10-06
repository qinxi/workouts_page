interface ISiteMetadataResult {
  siteTitle: string;
  siteUrl: string;
  description: string;
  keywords: string;
  logo: string;
  navLinks: {
    name: string;
    url: string;
  }[];
}

const data: ISiteMetadataResult = {
  siteTitle: 'Qinxi\'s Workouts Map',
  siteUrl: 'https://workout.qinxi1992.com/',
  logo: 'https://avatars.githubusercontent.com/u/6198010?v=4',
  description: 'Personal site and blog',
  keywords: 'workouts, running, cycling, riding, roadtrip, hiking, swimming',
  navLinks: [
    {
      name: 'About',
      url: 'https://github.com/qinxi/workouts_page/blob/master/README-CN.md',
    },
  ],
};

export default data;
